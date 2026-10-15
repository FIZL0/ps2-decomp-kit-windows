// A memory range as bytes / ints / floats, with whatever Ghidra already knows about it applied.
//
// Run by:  ./kit ghidra data <addr> [len]   (ps2kit/ghidra.py -> postscript("DumpData", [out, addr, len]))
//
// Args: <out> <hexaddr> <len> [as]      len is decimal or 0x…; `as` is one of
//       auto (default) | u8 | u16 | u32 | s32 | f32 | hex
//
// Why a Ghidra script when `./kit data` reads the same bytes out of the ELF with no JVM at all: half the
// work of a port is reading tuning tables out of the game image, and by the time you care you have usually
// already told Ghidra what the table *is* — a struct array, a float[4][8], a pointer table. This prints
// those defined data items with their labels, types and values first, and only then the raw grid. The
// offline `./kit data` gives you the grid alone.
//
// Output, per defined item then per row of raw data:
//     +0x0010  00322d10  someTable   float[4]   {1.0, 2.0, 0.5, 0.0}
//     +0x0010  00322d10  3f800000 40000000 3f000000 00000000   1.000000 2.000000 0.500000 0.000000
//@category PS2
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.data.DataType;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.mem.MemoryAccessException;
import ghidra.program.model.symbol.Symbol;

import java.io.File;
import java.io.PrintWriter;

public class DumpData extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        if (a.length < 3) {
            println("DumpData: need <out> <hexaddr> <len> [u8|u16|u32|s32|f32|hex|auto]");
            return;
        }
        File outFile = new File(a[0]);
        if (outFile.getParentFile() != null) outFile.getParentFile().mkdirs();
        long base = Long.parseLong(a[1].replaceFirst("^0[xX]", ""), 16);
        int len = (int) parseNum(a[2]);
        String as = a.length > 3 ? a[3] : "auto";
        Address start = toAddr(base);

        byte[] buf = new byte[len];
        int got;
        try {
            got = currentProgram.getMemory().getBytes(start, buf);
        } catch (MemoryAccessException e) {
            println("DumpData: " + a[1] + " is not in this program's memory (" + e.getMessage() + ")");
            return;
        }

        try (PrintWriter w = new PrintWriter(outFile, "UTF-8")) {
            Symbol s = getSymbolAt(start);
            w.println(String.format("# %s  %08x  %d bytes%s", currentProgram.getName(), base, got,
                    s == null ? "" : "  label " + s.getName()));

            int defined = 0;
            AddressSet range = new AddressSet(start, start.add(Math.max(0, got - 1)));
            DataIterator it = currentProgram.getListing().getDefinedData(range, true);
            while (it.hasNext() && !monitor.isCancelled()) {
                Data d = it.next();
                DataType t = d.getDataType();
                Symbol lab = getSymbolAt(d.getAddress());
                w.println(String.format("+0x%04x  %08x  %-20s %-16s %s",
                        d.getAddress().getOffset() - base, d.getAddress().getOffset(),
                        lab == null ? "" : lab.getName(), t == null ? "?" : t.getDisplayName(),
                        flat(d.getDefaultValueRepresentation())));
                defined++;
            }
            if (defined > 0) w.println();

            if (as.equals("u8") || as.equals("hex")) hexRows(w, base, buf, got);
            else if (as.equals("u16")) shortRows(w, base, buf, got);
            else wordRows(w, base, buf, got, as);
            w.println("# " + defined + " item(s) already typed in Ghidra");
        }
        println("DumpData -> " + outFile);
    }

    private void hexRows(PrintWriter w, long base, byte[] b, int n) {
        for (int i = 0; i < n; i += 16) {
            StringBuilder hex = new StringBuilder(), txt = new StringBuilder();
            for (int j = i; j < Math.min(i + 16, n); j++) {
                hex.append(String.format("%02x ", b[j] & 0xff));
                txt.append(b[j] >= 0x20 && b[j] < 0x7f ? (char) b[j] : '.');
            }
            w.println(String.format("+0x%04x  %08x  %-48s %s", i, base + i, hex, txt));
        }
    }

    private void shortRows(PrintWriter w, long base, byte[] b, int n) {
        for (int i = 0; i + 1 < n; i += 16) {
            StringBuilder row = new StringBuilder();
            for (int j = i; j + 1 < Math.min(i + 16, n); j += 2) row.append(String.format("%6d ", u16(b, j)));
            w.println(String.format("+0x%04x  %08x  %s", i, base + i, row));
        }
    }

    private void wordRows(PrintWriter w, long base, byte[] b, int n, String as) {
        for (int i = 0; i + 3 < n; i += 16) {
            StringBuilder hex = new StringBuilder(), num = new StringBuilder();
            for (int j = i; j + 3 < Math.min(i + 16, n); j += 4) {
                int v = u32(b, j);
                hex.append(String.format("%08x ", v));
                if (as.equals("u32")) num.append(String.format("%11d ", v & 0xffffffffL));
                else if (as.equals("s32")) num.append(String.format("%11d ", v));
                else if (as.equals("f32")) num.append(String.format("%14.6g ", Float.intBitsToFloat(v)));
                else num.append(plausible(v));                 // auto: show whichever reading looks real
            }
            w.println(String.format("+0x%04x  %08x  %-36s %s", i, base + i, hex, num));
        }
    }

    /** "auto": a word is shown as a float when its exponent is in the range real game data uses, else as
     *  a signed integer. 0x3f800000..0x47 and tiny values are floats; 0x0000002a is not. */
    private String plausible(int v) {
        float f = Float.intBitsToFloat(v);
        int exp = (v >> 23) & 0xff;
        boolean looksFloat = v == 0 || (exp > 0x60 && exp < 0x9e) || (exp > 0xe0 && exp < 0xfe);
        return looksFloat ? String.format("%14.6g ", f) : String.format("%14d ", v);
    }

    private int u16(byte[] b, int i) {
        return (b[i] & 0xff) | ((b[i + 1] & 0xff) << 8);
    }

    private int u32(byte[] b, int i) {
        return (b[i] & 0xff) | ((b[i + 1] & 0xff) << 8) | ((b[i + 2] & 0xff) << 16) | ((b[i + 3] & 0xff) << 24);
    }

    private long parseNum(String s) {
        s = s.trim();
        return s.toLowerCase().startsWith("0x") ? Long.parseLong(s.substring(2), 16) : Long.parseLong(s);
    }

    private String flat(String s) {
        return s == null ? "" : s.replaceAll("[\\t\\r\\n]+", " ");
    }
}
