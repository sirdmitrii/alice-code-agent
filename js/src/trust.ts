// Уровень доверия: all — спрашивать на всё, danger — только опасные (по умолч.),
// none — не спрашивать. Порт с Python.
import { DANGEROUS } from "./tools";

export const TRUST_MODES: Record<string, string> = {
  all: "подтверждать все операции",
  danger: "подтверждать только опасные (запись/правка/команды)",
  none: "не запрашивать подтверждения",
};

export class Trust {
  constructor(public mode: string = "danger") {}

  needsConfirm(name: string): boolean {
    if (this.mode === "none") return false;
    if (this.mode === "all") return true;
    return DANGEROUS.has(name); // режим danger
  }
}

export function argsPreview(argsJson: string): string {
  let args: any;
  try {
    args = JSON.parse(argsJson || "{}");
  } catch {
    args = {};
  }
  return Object.entries(args)
    .map(([k, v]) => `${k}=${String(v).slice(0, 60)}`)
    .join(", ");
}
