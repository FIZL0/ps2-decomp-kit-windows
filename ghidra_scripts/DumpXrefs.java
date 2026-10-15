// Every memory reference in the program except branches, as TSV: to, from, function-at-from, type.
//
// Run by:  ./kit ghidra dump          (ps2kit/ghidra.py -> postscript("DumpXrefs", [outdir]))
//
// Args: <out>  — a directory (then the file is <out>/<program>.xrefs.tsv) or a file path.
//
// Columns, one row per reference, addresses as 8 lowercase hex digits (ps2kit/decomp.py streams this and
// matches on column 1 for "who touches this address" and on column 2 for "what does this function touch"):
//
//     to        from      function   type
//     001000a0  002001b4  FUN_002001a0   UNCONDITIONAL_CALL
//
// Branches are skipped on purpose: intra-function jumps are 90% of the rows and answer no question you
// would ask a cross-reference table. Calls, reads and writes are what matter, and keeping the file small
// is what makes streaming it per query cheap.
//@category PS2
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

import java.io.File;
import java.io.PrintWriter;

public class DumpXrefs extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        if (a.length < 1) {
            println("DumpXrefs: need an output directory or file as the first argument");
            return;
        }
        String program = currentProgram.getName().replaceFirst("\\.elf$", "");
        File outFile = out(a[0], program + ".xrefs.tsv");
        long n = 0;
        try (PrintWriter w = new PrintWriter(outFile, "UTF-8")) {
            ReferenceIterator it = currentProgram.getReferenceManager()
                    .getReferenceIterator(currentProgram.getMinAddress());
            while (it.hasNext()) {
                if (monitor.isCancelled()) break;
                Reference r = it.next();
                if (r.getReferenceType().isJump()) continue;
                if (!r.getToAddress().isMemoryAddress() || !r.getFromAddress().isMemoryAddress()) continue;
                Function f = getFunctionContaining(r.getFromAddress());
                w.println(String.format("%08x\t%08x\t%s\t%s",
                        r.getToAddress().getOffset(), r.getFromAddress().getOffset(),
                        f == null ? "-" : f.getName(), r.getReferenceType()));
                if ((++n % 100000) == 0) monitor.setMessage("DumpXrefs " + n + " references");
            }
        }
        println("DumpXrefs: " + n + " references -> " + outFile);
    }

    private File out(String arg, String name) {
        File f = new File(arg);
        if (f.isDirectory() || arg.endsWith("/") || arg.endsWith(File.separator)) {
            f.mkdirs();
            return new File(f, name);
        }
        if (f.getParentFile() != null) f.getParentFile().mkdirs();
        return f;
    }
}
