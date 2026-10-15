// Instructions of the functions whose addresses are given — Ghidra's own disassembly, so the r5900's
// multimedia and VU macro-mode instructions come out right.
//
// Run by:  ./kit ghidra asm <addr>...   (ps2kit/ghidra.py -> postscript("DumpAsm", [out, addr...]))
//
// Args: <out> <hexaddr>...   — addresses without 0x. An address anywhere inside a function dumps the
// whole function; an address Ghidra has not made a function at dumps a 0x100-byte window instead of
// failing, because "not a function yet" is the normal state of a half-explored program.
//
// Each line is:   <address>  <bytes>  <mnemonic operands>        ; comment
//
// `./kit dis` is the offline equivalent (reads the ELF, decodes r5900 itself) and needs no Ghidra; use
// this one when you want Ghidra's labels, references and switch-table analysis in the output.
//@category PS2
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.CodeUnit;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;

import java.io.File;
import java.io.PrintWriter;

public class DumpAsm extends GhidraScript {
    private static final int WINDOW = 0x100;

    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        if (a.length < 2) {
            println("DumpAsm: need <out> and at least one hex address");
            return;
        }
        File outFile = new File(a[0]);
        if (outFile.getParentFile() != null) outFile.getParentFile().mkdirs();
        try (PrintWriter w = new PrintWriter(outFile, "UTF-8")) {
            for (int i = 1; i < a.length; i++) {
                Address at = toAddr(Long.parseLong(a[i].replaceFirst("^0[xX]", ""), 16));
                Function f = getFunctionContaining(at);
                InstructionIterator it;
                if (f != null) {
                    w.println(String.format("== %s %s  size=%d",
                            f.getName(), f.getEntryPoint(), f.getBody().getNumAddresses()));
                    it = currentProgram.getListing().getInstructions(f.getBody(), true);
                } else {
                    w.println("== " + at + "  (no function here; dumping " + WINDOW + " bytes)");
                    it = currentProgram.getListing().getInstructions(at, true);
                }
                long limit = at.getOffset() + WINDOW;
                while (it.hasNext()) {
                    if (monitor.isCancelled()) break;
                    Instruction ins = it.next();
                    if (f == null && ins.getAddress().getOffset() >= limit) break;
                    w.println(line(ins));
                }
                w.println();
            }
        }
        println("DumpAsm -> " + outFile);
    }

    private String line(Instruction ins) {
        StringBuilder bytes = new StringBuilder();
        try {
            for (byte b : ins.getBytes()) bytes.append(String.format("%02x", b));
        } catch (Exception e) {
            bytes.append("--------");
        }
        String comment = ins.getComment(CodeUnit.EOL_COMMENT);
        return String.format("%08x  %-8s  %s", ins.getAddress().getOffset(), bytes, ins)
                + (comment == null ? "" : "    ; " + comment.replaceAll("[\\r\\n]+", " "));
    }
}
