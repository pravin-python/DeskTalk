"""SDDL parsing and the Windows ACL safety gate of service/install_service.py (SPEC 10.2, 11 item 16)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import unittest
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_installer() -> Any:
    """Import service/install_service.py once per test process (it is a script, not a package)."""
    name = "desktalk_install_service"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "service", "install_service.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inst = load_installer()

SAFE = "O:BAG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;LS)(A;OICI;0x1200a9;;;BU)"
# What a default non-system drive root looks like: Authenticated Users may create/modify below it.
DRIVE_ROOT = "D:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICIIO;0x1301bf;;;AU)(A;;0x4;;;AU)(A;OICI;0x1200a9;;;BU)"


def names(sddl: str) -> List[str]:
    return [finding.name for finding in inst.unsafe_aces(sddl)]


class SddlParsingTests(unittest.TestCase):
    def test_safe_acl_has_no_findings(self) -> None:
        self.assertEqual(inst.unsafe_aces(SAFE), [])

    def test_default_drive_root_is_flagged(self) -> None:
        findings = inst.unsafe_aces(DRIVE_ROOT)
        self.assertEqual([f.name for f in findings], ["Authenticated Users", "Authenticated Users"])
        self.assertEqual(findings[0].sid, "S-1-5-11")
        self.assertIn("DELETE", inst.describe_mask(findings[0].mask))
        self.assertIn("FILE_WRITE_DATA/ADD_FILE", inst.describe_mask(findings[0].mask))
        self.assertIn("FILE_APPEND_DATA/ADD_SUBDIRECTORY", inst.describe_mask(findings[1].mask))

    def test_each_broad_trustee_by_alias_and_by_sid(self) -> None:
        for trustee, name in (
            ("WD", "Everyone"), ("S-1-1-0", "Everyone"),
            ("AU", "Authenticated Users"), ("S-1-5-11", "Authenticated Users"),
            ("BU", "Users"), ("S-1-5-32-545", "Users"),
            ("IU", "Interactive"), ("S-1-5-4", "Interactive"),
        ):  # fmt: skip
            self.assertEqual(names("D:(A;;0x2;;;%s)" % trustee), [name], trustee)

    def test_every_write_right_is_detected(self) -> None:
        for rights in ("0x2", "0x4", "0x10000", "0x40000", "0x80000", "0x40000000", "0x10000000", "0x1301bf",
                       "0x100116", "FA", "FW", "GA", "GW", "WD", "WO", "SD", "DC", "LC", "GRWD"):  # fmt: skip
            self.assertEqual(names("D:(A;;%s;;;BU)" % rights), ["Users"], rights)

    def test_read_and_execute_only_is_safe(self) -> None:
        for rights in ("0x1200a9", "FR", "FX", "GR", "GX", "GRGX", "RC", "0x120089", "0x1", "0x20000"):
            self.assertEqual(names("D:(A;OICI;%s;;;BU)(A;;%s;;;WD)" % (rights, rights)), [], rights)

    def test_other_trustees_and_ace_types_are_ignored(self) -> None:
        sddl = "D:(A;;FA;;;BA)(A;;FA;;;S-1-5-21-1-2-3-1001)(D;;FA;;;WD)(AU;SA;FA;;;WD)(A;;FA;;;LS)(A;;FA;;;CO)"
        self.assertEqual(inst.unsafe_aces(sddl), [])

    def test_inherit_only_and_container_flags_do_not_hide_a_finding(self) -> None:
        for flags in ("OICIIO", "CI", "OI", "OICI", "ID", ""):
            self.assertEqual(names("D:(A;%s;0x1301bf;;;BU)" % flags), ["Users"], flags)

    def test_components_owner_group_sacl(self) -> None:
        sddl = "O:BAG:SYD:PAI(A;;FA;;;SY)(A;;0x2;;;AU)S:AI(ML;;NW;;;LW)(AU;SA;FA;;;WD)"
        parts = inst.split_sddl(sddl)
        self.assertEqual(sorted(parts), ["D", "G", "O", "S"])
        self.assertEqual(parts["O"], "BA")
        self.assertEqual(parts["G"], "SY")
        self.assertEqual(names(sddl), ["Authenticated Users"])  # the SACL is never read as allow ACEs

    def test_conditional_ace_with_nested_parentheses(self) -> None:
        sddl = 'D:(XA;;FW;;;WD;(@User.Title=="PM"))(A;;FA;;;BA)'
        aces = inst.parse_dacl(sddl)
        self.assertEqual([a.kind for a in aces], ["XA", "A"])
        self.assertEqual(names(sddl), ["Everyone"])

    def test_malformed_input_never_raises(self) -> None:
        for sddl in ("", "garbage", "D:", "D:(A;;FA)", "D:((((", "D:)A;;FA;;;WD("):
            self.assertEqual(inst.unsafe_aces(sddl), [], sddl)

    def test_parse_rights(self) -> None:
        self.assertEqual(inst.parse_rights("0x1200a9"), 0x1200A9)
        self.assertEqual(inst.parse_rights("0X2"), 2)
        self.assertEqual(inst.parse_rights("123"), 123)
        self.assertEqual(inst.parse_rights("FA"), 0x1F01FF)
        self.assertEqual(inst.parse_rights("GRGX"), 0x80000000 | 0x20000000)
        self.assertEqual(inst.parse_rights("ZZ"), 0)
        self.assertEqual(inst.parse_rights("0xZZ"), 0)

    def test_describe_mask_names_only_write_rights(self) -> None:
        self.assertEqual(inst.describe_mask(0x1200A9), "0x1200a9: ")
        self.assertEqual(inst.describe_mask(0x40000002), "0x40000002: FILE_WRITE_DATA/ADD_FILE, GENERIC_WRITE")


# Verbatim `icacls <path> /save` output captured on Windows 10 (decoded UTF-16 LE, no BOM): name line, SDDL line.
REAL_DUMPS = {
    "python313_all_users": (
        "Python313\r\nD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)S:PAINO_ACCESS_CONTROL\r\n",
        [],
    ),
    "program_files": (
        "Program Files\r\n"
        "D:PAI(A;;FA;;;S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464)"
        "(A;CIIO;GA;;;S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464)(A;;0x1301bf;;;SY)"
        "(A;OICIIO;GA;;;SY)(A;;0x1301bf;;;BA)(A;OICIIO;GA;;;BA)(A;;0x1200a9;;;BU)(A;OICIIO;GXGR;;;BU)"
        "(A;OICIIO;GA;;;CO)(A;;0x1200a9;;;AC)(A;OICIIO;GXGR;;;AC)(A;;0x1200a9;;;S-1-15-2-2)"
        "(A;OICIIO;GXGR;;;S-1-15-2-2)\r\n",
        [],
    ),
    "default_e_drive_root": (
        "\r\n"  # the root of a drive has an empty object name
        "D:(A;;FA;;;BA)(A;OICIIO;GA;;;BA)(A;;FA;;;SY)(A;OICIIO;GA;;;SY)(A;;0x1301bf;;;AU)"
        "(A;OICIIO;SDGXGWGR;;;AU)(A;;0x1200a9;;;BU)(A;OICIIO;GXGR;;;BU)\r\n",
        ["Authenticated Users", "Authenticated Users"],
    ),
    "checkout_on_e_drive": (
        "free-chat\r\n"
        "D:(A;ID;FA;;;BA)(A;OICIIOID;GA;;;BA)(A;ID;FA;;;SY)(A;OICIIOID;GA;;;SY)(A;ID;0x1301bf;;;AU)"
        "(A;OICIIOID;SDGXGWGR;;;AU)(A;ID;0x1200a9;;;BU)(A;OICIIOID;GXGR;;;BU)\r\n",
        ["Authenticated Users", "Authenticated Users"],
    ),
    "program_data": (
        "ProgramData\r\n"
        "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIIO;GA;;;CO)(A;OICI;0x1200a9;;;BU)(A;CI;DCLCRPCR;;;BU)\r\n",
        ["Users"],
    ),
}


class RealIcaclsDumpTests(unittest.TestCase):
    def test_real_dumps_are_utf16_without_bom_and_classified_correctly(self) -> None:
        for label, (text, expected) in REAL_DUMPS.items():
            raw = text.encode("utf-16-le")
            self.assertFalse(raw.startswith((b"\xff\xfe", b"\xfe\xff")), label)
            sddl = inst.extract_sddl(raw)
            self.assertIsNotNone(sddl, label)
            self.assertEqual([f.name for f in inst.unsafe_aces(sddl)], expected, label)

    def test_a_dump_that_starts_with_the_drive_root_name_is_not_mistaken_for_sddl(self) -> None:
        raw = ("D:\\\r\n" + REAL_DUMPS["python313_all_users"][0].split("\r\n", 1)[1]).encode("utf-16-le")
        self.assertTrue(inst.extract_sddl(raw).startswith("D:PAI("))

    def test_decoder_variants(self) -> None:
        text = "x\r\nD:PAI(A;;FA;;;SY)\r\n"
        for raw in (text.encode("utf-16-le"), text.encode("utf-16"), text.encode("utf-8"), text.encode("utf-8-sig")):
            self.assertEqual(inst.extract_sddl(raw), "D:PAI(A;;FA;;;SY)", raw[:8])


class ExtractSddlTests(unittest.TestCase):
    def test_utf16_with_bom(self) -> None:
        raw = ("\ufeff" + "app\r\n" + SAFE + "\r\n").encode("utf-16-le")
        self.assertEqual(inst.extract_sddl(raw), SAFE)

    def test_utf8_and_object_name_line_is_skipped(self) -> None:
        raw = ("D:\\odd name\r\n" + DRIVE_ROOT + "\r\n").encode("utf-8")
        self.assertEqual(inst.extract_sddl(raw), DRIVE_ROOT)

    def test_without_an_acl_line(self) -> None:
        self.assertIsNone(inst.extract_sddl(b"nothing useful\r\n"))
        self.assertIsNone(inst.extract_sddl(b""))


class AclContext(inst.Context):
    """A Windows context whose ``icacls`` is a dictionary of SDDL strings; hardening makes a path safe."""

    def __init__(self, sddl_by_path: Dict[str, str], dry_run: bool = False) -> None:
        super().__init__("windows", dry_run=dry_run, host="windows", env={}, interactive=False)
        self.sddl_by_path = dict(sddl_by_path)
        self.commands: List[List[str]] = []
        self.harden_works = True

    def run(self, argv: Any, **kwargs: Any) -> Any:
        command = [str(a) for a in argv]
        if self.dry_run:
            return super().run(command, **kwargs)
        self.commands.append(command)
        if command[0] == "icacls" and "/save" in command:
            text = "name\r\n" + self.sddl_by_path[command[1]] + "\r\n"  # real icacls: UTF-16 LE, no BOM
            with open(command[3], "wb") as handle:
                handle.write(text.encode("utf-16-le"))
        elif command[0] == "icacls" and "/grant:r" in command and self.harden_works:
            self.sddl_by_path[command[1]] = SAFE
        return inst.Result(0, "", "")

    def hardened(self) -> List[str]:
        return [c[1] for c in self.commands if c[0] == "icacls" and "/grant:r" in c]


class AclGateTests(unittest.TestCase):
    def gate(self, ctx: AclContext, paths: List[str], harden: bool) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            inst.WindowsBackend(ctx).acl_gate(paths, harden)

    def test_safe_paths_pass_without_changes(self) -> None:
        ctx = AclContext({"C:\\app": SAFE, "C:\\py": SAFE})
        self.gate(ctx, ["C:\\app", "C:\\py"], harden=False)
        self.assertEqual(ctx.hardened(), [])

    def test_unsafe_path_is_refused_with_exit_2_and_prints_the_hardening_command(self) -> None:
        ctx = AclContext({"E:\\app": DRIVE_ROOT, "C:\\py": SAFE})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(inst.InstallerError) as caught:
            inst.WindowsBackend(ctx).acl_gate(["E:\\app", "C:\\py"], False)
        self.assertEqual(caught.exception.code, inst.EXIT_USAGE)
        message = str(caught.exception)
        self.assertIn("--harden", message)
        self.assertIn("icacls E:\\app /inheritance:r /grant:r", message)
        self.assertNotIn("C:\\py", message)  # only the failing path is listed
        self.assertIn("UNSAFE: E:\\app: Authenticated Users can write", out.getvalue())
        self.assertEqual(ctx.hardened(), [])  # never hardens without --harden

    def test_harden_applies_to_exactly_the_failing_paths_and_rechecks(self) -> None:
        ctx = AclContext({"E:\\app": DRIVE_ROOT, "C:\\py": DRIVE_ROOT, "D:\\ok": SAFE})
        self.gate(ctx, ["E:\\app", "C:\\py", "D:\\ok", "E:\\app"], harden=True)
        self.assertEqual(ctx.hardened(), ["E:\\app", "C:\\py"])
        saves = [c[1] for c in ctx.commands if "/save" in c]
        self.assertEqual(saves, ["E:\\app", "C:\\py", "D:\\ok", "E:\\app", "C:\\py"])  # duplicates dropped, re-check

    def test_harden_that_does_not_help_still_refuses(self) -> None:
        ctx = AclContext({"E:\\app": DRIVE_ROOT})
        ctx.harden_works = False
        with self.assertRaises(inst.InstallerError) as caught:
            self.gate(ctx, ["E:\\app"], harden=True)
        self.assertEqual(caught.exception.code, inst.EXIT_USAGE)

    def test_icacls_failure_is_an_error_not_a_refusal(self) -> None:
        ctx = AclContext({"E:\\app": SAFE})
        original = ctx.run

        def failing(argv: Any, **kwargs: Any) -> Any:
            if "/save" in list(argv):
                return inst.Result(5, "", "Access is denied.")
            return original(argv, **kwargs)

        ctx.run = failing  # type: ignore[method-assign]
        with self.assertRaises(inst.InstallerError) as caught:
            self.gate(ctx, ["E:\\app"], harden=False)
        self.assertEqual(caught.exception.code, inst.EXIT_ERROR)
        self.assertIn("Access is denied", str(caught.exception))

    def test_dry_run_prints_the_commands_and_changes_nothing(self) -> None:
        ctx = AclContext({}, dry_run=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inst.WindowsBackend(ctx).acl_gate(["C:\\app"], harden=True)
        text = out.getvalue()
        self.assertIn("[dry-run] $ icacls C:\\app /save", text)
        self.assertIn("/inheritance:r /grant:r", text)
        self.assertEqual(ctx.commands, [])


if __name__ == "__main__":
    unittest.main()
