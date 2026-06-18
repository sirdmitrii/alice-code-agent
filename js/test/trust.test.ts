import { describe, expect, it } from "vitest";
import { Trust } from "../src/trust";

describe("Trust.needsConfirm", () => {
  it("danger: write да, read нет", () => {
    const t = new Trust("danger");
    expect(t.needsConfirm("write_file")).toBe(true);
    expect(t.needsConfirm("read_file")).toBe(false);
  });
  it("all: даже read спрашивает", () => {
    expect(new Trust("all").needsConfirm("read_file")).toBe(true);
  });
  it("none: write не спрашивает", () => {
    expect(new Trust("none").needsConfirm("write_file")).toBe(false);
  });
});
