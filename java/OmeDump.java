import java.io.DataOutputStream;
import java.io.BufferedOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Hashtable;
import java.util.List;
import java.util.regex.Pattern;

import loci.common.DebugTools;
import loci.common.services.ServiceFactory;
import loci.formats.FormatTools;
import loci.formats.ImageReader;
import ome.xml.model.primitives.Color;
import loci.formats.meta.IMetadata;
import loci.formats.services.OMEXMLService;

/**
 * Read-only batch metadata dumper for the microscopy catalog.
 *
 * Usage: java -cp <build>;<bioformats lib>/* OmeDump <jobs.tsv>
 *
 * Each job line is tab separated:  mode, input path, output path [, key regex]
 *   mode "ome"  -> writes OME-XML (UTF-8) to output and the list of used files to output + ".used.txt"
 *   mode "orig" -> writes "series \t key \t value" lines of original metadata whose key matches the regex
 *   mode "thumb" -> preview data for the comma separated series in the 4th column: per series
 *                  output + "_s<series>.json" (layout) and output + "_s<series>.bin" (float32 LE):
 *                  per channel: overview mid-Z, overview max-Z, cutout mid-Z, cutout max-Z (first timepoint)
 *
 * The input files are only opened for reading. No Memoizer is used, so no .bfmemo
 * cache files are created next to the data. Progress lines go to stdout (UTF-8):
 *   START <i>
 *   DONE <i> OK <millis> <seriesCount>
 *   DONE <i> ERR <message>
 */
public class OmeDump {

    public static void main(String[] args) throws Exception {
        DebugTools.setRootLevel("OFF");
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        List<String> lines = Files.readAllLines(Paths.get(args[0]), StandardCharsets.UTF_8);
        ServiceFactory factory = new ServiceFactory();
        OMEXMLService service = factory.getInstance(OMEXMLService.class);

        for (int i = 0; i < lines.size(); i++) {
            String line = lines.get(i);
            if (line.trim().isEmpty()) {
                continue;
            }
            String[] f = line.split("\t", -1);
            out.println("START " + i);
            long t0 = System.currentTimeMillis();
            ImageReader reader = new ImageReader();
            try {
                String mode = f[0];
                String src = f[1];
                String dst = f[2];
                reader.setFlattenedResolutions(false);
                if (mode.equals("ome")) {
                    IMetadata meta = service.createOMEXMLMetadata();
                    reader.setOriginalMetadataPopulated(false);
                    reader.setMetadataStore(meta);
                    reader.setId(src);
                    String[] used = reader.getUsedFiles();
                    int series = reader.getSeriesCount();
                    String xml = service.getOMEXML(meta);
                    writeAtomic(dst, xml);
                    writeAtomic(dst + ".used.txt", String.join("\n", used));
                    out.println("DONE " + i + " OK " + (System.currentTimeMillis() - t0) + " " + series);
                } else if (mode.equals("orig")) {
                    Pattern pat = Pattern.compile(f.length > 3 && !f[3].isEmpty() ? f[3] : ".*");
                    reader.setOriginalMetadataPopulated(true);
                    reader.setId(src);
                    StringBuilder sb = new StringBuilder();
                    append(sb, -1, reader.getGlobalMetadata(), pat);
                    int series = reader.getSeriesCount();
                    for (int s = 0; s < series; s++) {
                        reader.setSeries(s);
                        append(sb, s, reader.getSeriesMetadata(), pat);
                    }
                    writeAtomic(dst, sb.toString());
                    out.println("DONE " + i + " OK " + (System.currentTimeMillis() - t0) + " " + series);
                } else if (mode.equals("thumb")) {
                    IMetadata meta = service.createOMEXMLMetadata();
                    reader.setOriginalMetadataPopulated(false);
                    reader.setMetadataStore(meta);
                    reader.setId(src);
                    int n = 0;
                    for (String ss : (f.length > 3 ? f[3] : "").split(",")) {
                        if (ss.trim().isEmpty()) {
                            continue;
                        }
                        int s = Integer.parseInt(ss.trim());
                        if (s < 0 || s >= reader.getSeriesCount()) {
                            continue;
                        }
                        thumb(reader, meta, s, dst + "_s" + s);
                        n++;
                    }
                    out.println("DONE " + i + " OK " + (System.currentTimeMillis() - t0) + " " + n);
                } else {
                    out.println("DONE " + i + " ERR unknown mode " + mode);
                }
            } catch (Throwable e) {
                out.println("DONE " + i + " ERR " + clean(e.toString()));
            } finally {
                try {
                    reader.close();
                } catch (Throwable e) {
                    // ignore
                }
            }
        }
        out.flush();
    }

    private static void append(StringBuilder sb, int series, Hashtable<String, Object> table, Pattern pat) {
        if (table == null) {
            return;
        }
        List<String> keys = new ArrayList<>(table.keySet());
        Collections.sort(keys);
        for (String k : keys) {
            if (!pat.matcher(k).find()) {
                continue;
            }
            Object v = table.get(k);
            sb.append(series).append('\t').append(clean(k)).append('\t').append(clean(String.valueOf(v))).append('\n');
        }
    }

    // ------------------------------------------------------------------ previews
    static final int OV = 384;            // overview: longest side (pixels) before final scaling
    static final int CUT = 192;           // cutout size at full resolution
    static final int MAXZ = 10;           // at most this many Z planes (evenly spaced) for the max projection
    // max bytes read for the overview of one series (-Dmdcat.readBudget, set from previews = network|full);
    // at least one plane is always read
    static final long BUDGET = Long.getLong("mdcat.readBudget", 3_000_000_000L);
    static final long STRIP_BYTES = 64L << 20;

    private static void thumb(ImageReader r, IMetadata meta, int s, String prefix) throws Exception {
        r.setSeries(s);
        r.setResolution(0);
        int x0 = r.getSizeX(), y0 = r.getSizeY(), sizeZ = r.getSizeZ();
        int effC = r.getEffectiveSizeC(), rgb = r.getRGBChannelCount();
        int nch = Math.min(effC * rgb, 8);
        int bpp = FormatTools.getBytesPerPixel(r.getPixelType());
        // overview level: smallest pyramid level that is still >= OV on its longest side
        int lvl = 0;
        for (int k = 0; k < r.getResolutionCount(); k++) {
            r.setResolution(k);
            if (Math.max(r.getSizeX(), r.getSizeY()) >= OV) {
                lvl = k;
            }
        }
        r.setResolution(lvl);
        int lx = r.getSizeX(), ly = r.getSizeY();
        long planeBytes = (long) lx * ly * bpp * rgb;
        int zBudget = (int) Math.max(1, Math.min(MAXZ, BUDGET / Math.max(1, planeBytes * effC)));
        List<Integer> zs = sample(sizeZ, zBudget);
        int zmid = sizeZ / 2;
        if (!zs.contains(zmid)) {
            zs.add(zmid);
            Collections.sort(zs);
        }
        // --- overview (at level lvl) ---
        int fac = Math.max(1, (int) Math.ceil(Math.max(lx, ly) / (double) OV));
        int ow = (lx + fac - 1) / fac, oh = (ly + fac - 1) / fac;
        float[][] ovMid = new float[nch][ow * oh], ovMax = new float[nch][ow * oh];
        for (float[] a : ovMax) java.util.Arrays.fill(a, Float.NEGATIVE_INFINITY);
        for (int z : zs) {
            for (int ce = 0; ce < effC; ce++) {
                if (ce * rgb >= nch) {
                    break;
                }
                float[][] ds = downsample(r, r.getIndex(z, ce, 0), lx, ly, fac, ow, oh, rgb, bpp);
                for (int k = 0; k < rgb; k++) {
                    int ch = ce * rgb + k;
                    if (ch >= nch) {
                        break;
                    }
                    for (int p = 0; p < ds[k].length; p++) {
                        if (ds[k][p] > ovMax[ch][p]) {
                            ovMax[ch][p] = ds[k][p];
                        }
                    }
                    if (z == zmid) {
                        ovMid[ch] = ds[k];
                    }
                }
            }
        }
        // --- cutout (full resolution, image centre) ---
        r.setResolution(0);
        int cw = Math.min(CUT, x0), chh = Math.min(CUT, y0);
        // place the cutout where the overview shows most structure (not into empty background)
        double sx = (double) x0 / lx, sy = (double) y0 / ly;
        int[] best = pickCutout(ovMax, ow, oh, Math.max(1, (int) Math.round(cw / sx / fac)),
                                Math.max(1, (int) Math.round(chh / sy / fac)));
        int cx = (int) Math.round(best[0] * fac * sx), cy = (int) Math.round(best[1] * fac * sy);
        cx = Math.max(0, Math.min(x0 - cw, cx));
        cy = Math.max(0, Math.min(y0 - chh, cy));
        float[][] cuMid = new float[nch][cw * chh], cuMax = new float[nch][cw * chh];
        for (float[] a : cuMax) java.util.Arrays.fill(a, Float.NEGATIVE_INFINITY);
        List<Integer> czs = sample(sizeZ, MAXZ);
        if (!czs.contains(zmid)) {
            czs.add(zmid);
        }
        for (int z : czs) {
            for (int ce = 0; ce < effC; ce++) {
                if (ce * rgb >= nch) {
                    break;
                }
                byte[] b = r.openBytes(r.getIndex(z, ce, 0), cx, cy, cw, chh);
                for (int k = 0; k < rgb; k++) {
                    int ch = ce * rgb + k;
                    if (ch >= nch) {
                        break;
                    }
                    for (int p = 0; p < cw * chh; p++) {
                        float v = value(r, b, p, k, cw * chh, rgb, bpp);
                        if (v > cuMax[ch][p]) {
                            cuMax[ch][p] = v;
                        }
                        if (z == zmid) {
                            cuMid[ch][p] = v;
                        }
                    }
                }
            }
        }
        // --- write ---
        try (DataOutputStream o = new DataOutputStream(new BufferedOutputStream(
                Files.newOutputStream(Paths.get(prefix + ".bin.part")), 1 << 20))) {
            java.nio.ByteBuffer bb = java.nio.ByteBuffer.allocate(4).order(java.nio.ByteOrder.LITTLE_ENDIAN);
            for (int ch = 0; ch < nch; ch++) {
                for (float[] arr : new float[][] {ovMid[ch], ovMax[ch], cuMid[ch], cuMax[ch]}) {
                    for (float v : arr) {
                        bb.clear();
                        bb.putFloat(v);
                        o.write(bb.array());
                    }
                }
            }
        }
        Files.move(Paths.get(prefix + ".bin.part"), Paths.get(prefix + ".bin"), StandardCopyOption.REPLACE_EXISTING);
        StringBuilder js = new StringBuilder();
        js.append("{\"series\":").append(s).append(",\"X\":").append(x0).append(",\"Y\":").append(y0)
          .append(",\"Z\":").append(sizeZ).append(",\"T\":").append(r.getSizeT()).append(",\"nch\":").append(nch)
          .append(",\"rgb\":").append(rgb).append(",\"level\":").append(lvl).append(",\"factor\":").append(fac)
          .append(",\"ov_w\":").append(ow).append(",\"ov_h\":").append(oh)
          .append(",\"cut_w\":").append(cw).append(",\"cut_h\":").append(chh)
          .append(",\"cut_x\":").append(cx).append(",\"cut_y\":").append(cy)
          .append(",\"n_z_used\":").append(zs.size()).append(",\"names\":[");
        for (int ch = 0; ch < nch; ch++) {
            int ce = ch / rgb;
            String nm = null;
            try {
                nm = meta.getChannelName(s, ce);
            } catch (Throwable e) {
                // ignore
            }
            js.append(ch > 0 ? "," : "").append(nm == null ? "null" : "\"" + clean(nm).replace("\\", "/").replace("\"", "'") + "\"");
        }
        js.append("],\"colors\":[");
        for (int ch = 0; ch < nch; ch++) {
            int ce = ch / rgb;
            Color c = null;
            try {
                c = meta.getChannelColor(s, ce);
            } catch (Throwable e) {
                // ignore
            }
            js.append(ch > 0 ? "," : "").append(c == null || rgb > 1 ? "null" : String.valueOf(c.getValue()));
        }
        js.append("]}");
        writeAtomic(prefix + ".json", js.toString());
    }

    /**
     * Top-left corner (overview pixels) of the w x h window that contains the most signal.
     * Per channel the overview (max projection) is normalised to its 1..99.5 percentiles;
     * fluorescence-like channels (dark background) contribute their intensity, brightfield-like
     * channels (bright background, median > 0.35) their deviation from the background level.
     * The window with the largest summed signal wins, so even a mostly empty image shows
     * whatever is present (e.g. the corner of a tile).
     */
    private static int[] pickCutout(float[][] ov, int ow, int oh, int w, int h) {
        w = Math.min(w, ow);
        h = Math.min(h, oh);
        double[] score = new double[ow * oh];
        for (float[] ch : ov) {
            float[] s = ch.clone();
            java.util.Arrays.sort(s);
            int n1 = s.length - 1;
            float lo = s[(int) (0.01 * n1)], hi = s[(int) (0.995 * n1)], med = s[n1 / 2];
            if (!(hi > lo) || !Float.isFinite(lo) || !Float.isFinite(hi)) {
                continue;
            }
            double m = (med - lo) / (hi - lo);
            boolean bright = m > 0.35;
            for (int p = 0; p < score.length; p++) {
                double v = Math.max(0, Math.min(1, (ch[p] - lo) / (hi - lo)));
                score[p] += bright ? Math.abs(v - m) : Math.max(0, v - m);
            }
        }
        double[] ii = new double[(ow + 1) * (oh + 1)];
        for (int y = 0; y < oh; y++) {
            double r = 0;
            for (int x = 0; x < ow; x++) {
                r += score[y * ow + x];
                ii[(y + 1) * (ow + 1) + x + 1] = ii[y * (ow + 1) + x + 1] + r;
            }
        }
        double bestV = -1;
        int bx = (ow - w) / 2, by = (oh - h) / 2;
        for (int y = 0; y + h <= oh; y++) {
            for (int x = 0; x + w <= ow; x++) {
                int a = y * (ow + 1) + x, b = a + w, c = (y + h) * (ow + 1) + x, d = c + w;
                double sum = ii[d] - ii[b] - ii[c] + ii[a];
                if (sum > bestV + 1e-9) {
                    bestV = sum;
                    bx = x;
                    by = y;
                }
            }
        }
        return new int[] {bx, by};
    }

    private static List<Integer> sample(int n, int k) {
        List<Integer> out = new ArrayList<>();
        if (n <= k) {
            for (int i = 0; i < n; i++) out.add(i);
            return out;
        }
        for (int i = 0; i < k; i++) {
            int z = (int) Math.round(i * (n - 1) / (double) (k - 1));
            if (!out.contains(z)) out.add(z);
        }
        return out;
    }

    /** Block-mean downsampling of one plane, read in strips so huge planes need little memory. */
    private static float[][] downsample(ImageReader r, int idx, int w, int h, int fac, int ow, int oh,
                                        int rgb, int bpp) throws Exception {
        double[][] sum = new double[rgb][ow * oh];
        int[] cnt = new int[ow * oh];
        long rowBytes = (long) w * bpp * rgb;
        int strip = (int) Math.max(fac, (STRIP_BYTES / Math.max(1, rowBytes)) / fac * fac);
        for (int y = 0; y < h; y += strip) {
            int sh = Math.min(strip, h - y);
            byte[] b = r.openBytes(idx, 0, y, w, sh);
            int npix = w * sh;
            for (int yy = 0; yy < sh; yy++) {
                int orow = ((y + yy) / fac) * ow;
                for (int xx = 0; xx < w; xx++) {
                    int p = yy * w + xx, op = orow + xx / fac;
                    for (int k = 0; k < rgb; k++) {
                        sum[k][op] += value(r, b, p, k, npix, rgb, bpp);
                    }
                    cnt[op]++;
                }
            }
        }
        float[][] outv = new float[rgb][ow * oh];
        for (int k = 0; k < rgb; k++) {
            for (int p = 0; p < ow * oh; p++) {
                outv[k][p] = cnt[p] > 0 ? (float) (sum[k][p] / cnt[p]) : 0f;
            }
        }
        return outv;
    }

    private static float value(ImageReader r, byte[] b, int p, int k, int npix, int rgb, int bpp) {
        int off = (r.isInterleaved() ? p * rgb + k : k * npix + p) * bpp;
        boolean le = r.isLittleEndian();
        int type = r.getPixelType();
        switch (bpp) {
            case 1:
                return type == FormatTools.INT8 ? b[off] : (b[off] & 0xFF);
            case 2: {
                int v = le ? (b[off] & 0xFF) | ((b[off + 1] & 0xFF) << 8) : ((b[off] & 0xFF) << 8) | (b[off + 1] & 0xFF);
                return type == FormatTools.INT16 ? (short) v : v;
            }
            case 4: {
                int v = le ? (b[off] & 0xFF) | ((b[off + 1] & 0xFF) << 8) | ((b[off + 2] & 0xFF) << 16) | ((b[off + 3] & 0xFF) << 24)
                           : ((b[off] & 0xFF) << 24) | ((b[off + 1] & 0xFF) << 16) | ((b[off + 2] & 0xFF) << 8) | (b[off + 3] & 0xFF);
                if (type == FormatTools.FLOAT) return Float.intBitsToFloat(v);
                return type == FormatTools.INT32 ? v : (float) (v & 0xFFFFFFFFL);
            }
            case 8: {
                long v = 0;
                for (int i = 0; i < 8; i++) {
                    int sh = le ? 8 * i : 8 * (7 - i);
                    v |= ((long) (b[off + i] & 0xFF)) << sh;
                }
                return type == FormatTools.DOUBLE ? (float) Double.longBitsToDouble(v) : (float) v;
            }
            default:
                return 0f;
        }
    }

    private static String clean(String s) {
        return s == null ? "" : s.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ');
    }

    private static void writeAtomic(String dst, String text) throws Exception {
        Path target = Paths.get(dst);
        Path tmp = Paths.get(dst + ".part");
        Files.write(tmp, text.getBytes(StandardCharsets.UTF_8));
        Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
    }
}
