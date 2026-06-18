import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { safePath, setProjectDir, toolGlob, toolGrep } from "../src/tools";

let tmp: string;

beforeAll(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "alice-tools-"));
  fs.mkdirSync(path.join(tmp, "sub"));
  fs.mkdirSync(path.join(tmp, ".venv"));
  fs.writeFileSync(path.join(tmp, "a.py"), "def foo():\n    pass\n");
  fs.writeFileSync(path.join(tmp, "sub", "b.py"), "x = 1\nfoo bar baz\n");
  fs.writeFileSync(path.join(tmp, "sub", "c.txt"), "FOO upper case\n");
  fs.writeFileSync(path.join(tmp, ".venv", "junk.py"), "foo noise in venv\n");
  fs.writeFileSync(path.join(tmp, "data.bin"), Buffer.from([0, 1, 102, 111, 111, 0]));
  setProjectDir(tmp);
});

afterAll(() => fs.rmSync(tmp, { recursive: true, force: true }));

describe("glob", () => {
  it("находит .py и пропускает .venv", () => {
    const g = toolGlob({ pattern: "**/*.py" });
    expect(g).toContain("a.py");
    expect(g).toContain("sub/b.py");
    expect(g).not.toContain("junk.py");
  });
});

describe("grep", () => {
  it("находит в a.py и sub/b.py", () => {
    const r = toolGrep({ pattern: "foo" });
    expect(r).toContain("a.py:1:");
    expect(r).toContain("sub/b.py:2:");
  });
  it("пропускает .venv и бинарник", () => {
    const r = toolGrep({ pattern: "foo" });
    expect(r).not.toContain("junk.py");
    expect(r).not.toContain("data.bin");
  });
  it("регистрозависим по умолчанию (нет FOO)", () => {
    expect(toolGrep({ pattern: "foo" })).not.toContain("c.txt");
  });
  it("ignore_case ловит FOO", () => {
    expect(toolGrep({ pattern: "foo", ignore_case: true })).toContain("c.txt:1:");
  });
  it("include='*.py' только .py", () => {
    const r = toolGrep({ pattern: "foo", include: "*.py" });
    expect(r).toContain("a.py:1:");
    expect(r).not.toContain("c.txt");
  });
});

describe("песочница", () => {
  it("блокирует выход за рабочую папку", () => {
    expect(() => safePath("../secrets.txt")).toThrow();
  });
});
