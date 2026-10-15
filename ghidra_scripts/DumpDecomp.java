// Decompile every function in the current program to one .c file, plus a table of functions.
//
// Run by:  ./kit ghidra dump          (ps2kit/ghidra.py -> postscript("DumpDecomp", [outdir]))
//
// Args: <out>  — a directory (then the files are <out>/<program>.c and <out>/<program>.funcs.tsv)
//                or a file path (then .funcs.tsv sits beside it).
//
// The header line before each function is a contract with ps2kit/decomp.py, which finds a function by
// streaming this file and stopping at the next header — it never loads the dump, which is 10-30 MB:
//
//     // 001000a0 FUN_001000a0 size=1234
//
// i.e. "// " then the entry point as exactly 8 lowercase hex digits, a space, the name, a space, size=N.
// Change that format and `./kit fn` stops finding anything. The address is formatted here rather than
// taken from Address.toString() so the width never depends on Ghidra's address-space formatting.
//@category PS2
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;

import java.io.File;
import java.io.PrintWriter;

public class DumpDecomp extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        if (a.length < 1) {
            println("DumpDecomp: need an output directory or file as the first argument");
            return;
        }
        String program = currentProgram.getName().replaceFirst("\\.elf$", "");
        File cFile = out(a[0], program, ".c");
        File tFile = cFile.getName().endsWith(".c")
                ? new File(cFile.getParentFile(), cFile.getName().replaceFirst("\\.c$", ".funcs.tsv"))
                : new File(cFile.getPath() + ".funcs.tsv");

        DecompInterface d = new DecompInterface();
        DecompileOptions opts = new DecompileOptions();
        d.setOptions(opts);
        if (!d.openProgram(currentProgram)) {
            println("DumpDecomp: decompiler would not open the program: " + d.getLastMessage());
            return;
        }
        int n = 0, failed = 0;
        try (PrintWriter c = new PrintWriter(cFile, "UTF-8");
             PrintWriter t = new PrintWriter(tFile, "UTF-8")) {
            c.println("// " + program + ": every function, decompiled. Do not read this file whole —");
            c.println("// it is tens of megabytes. Use `./kit fn <address>` / `./kit xref <address>`.");
            for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
                if (monitor.isCancelled()) break;
                long ep = f.getEntryPoint().getOffset();
                long size = f.getBody().getNumAddresses();
                DecompileResults r = d.decompileFunction(f, 60, monitor);
                String code;
                if (r.decompileCompleted() && r.getDecompiledFunction() != null) {
                    code = r.getDecompiledFunction().getC();
                } else {
                    code = "/* decompile failed: " + r.getErrorMessage() + " */\n";
                    failed++;
                }
                c.println(String.format("// %08x %s size=%d", ep, f.getName(), size));
                c.println(code);
                t.println(String.format("%08x\t%s\t%d\t%s", ep, f.getName(), size, flat(signature(f))));
                n++;
                if ((n % 500) == 0) monitor.setMessage("DumpDecomp " + n + " functions");
            }
        } finally {
            d.dispose();
        }
        println("DumpDecomp: " + n + " functions (" + failed + " failed) -> " + cFile + ", " + tFile);
    }

    private String signature(Function f) {
        try {
            return f.getSignature().getPrototypeString(false);
        } catch (Throwable e) {
            return f.getName() + "()";
        }
    }

    /** One TSV field: no tabs, no newlines, no runs of spaces. */
    private String flat(String s) {
        return s == null ? "" : s.replaceAll("[\\t\\r\\n]+", " ").replaceAll("  +", " ").trim();
    }

    private File out(String arg, String program, String ext) {
        File f = new File(arg);
        if (f.isDirectory() || arg.endsWith("/") || arg.endsWith(File.separator)) {
            f.mkdirs();
            return new File(f, program + ext);
        }
        if (f.getParentFile() != null) f.getParentFile().mkdirs();
        return f;
    }
}
