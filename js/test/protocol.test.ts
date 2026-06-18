import { describe, it, expect } from "vitest";
import { abbrevArgs, parseToolCalls, renderMessagesToPrompt } from "../src/protocol";

const argsOf = (c: any) => JSON.parse(c.function.arguments);

describe("parseToolCalls — протокол с телами", () => {
  it("write_file: тело дословно (кавычки/```/\\/\\n)", () => {
    const body =
      'def f():\n    s = "он сказал \\"привет\\" и ```код```"\n    p = "C:\\\\new\\\\x"\n    return 1';
    const text =
      "Готово, сохраняю.\n```tool_call\n" +
      '{"name": "write_file", "arguments": {"path": "f.py", "content": "@@content@@"}}\n```\n' +
      "@@content@@\n" + body + "\n@@end@@\n";
    const { clean, toolCalls } = parseToolCalls(text);
    expect(toolCalls.length).toBe(1);
    expect(toolCalls[0].function.name).toBe("write_file");
    expect(argsOf(toolCalls[0]).content).toBe(body);
    expect(argsOf(toolCalls[0]).path).toBe("f.py");
    expect(clean).toContain("Готово");
    expect(clean).not.toContain("@@content@@");
    expect(clean).not.toContain("def f");
  });

  it("edit_file: два тела по именам old/new", () => {
    const text =
      "```tool_call\n" +
      '{"name":"edit_file","arguments":{"path":"a.py","old":"@@old@@","new":"@@new@@"}}\n```\n' +
      "@@old@@\ndef foo(): pass\n@@end@@\n@@new@@\ndef foo(): return 42\n@@end@@\n";
    const { toolCalls } = parseToolCalls(text);
    expect(argsOf(toolCalls[0]).old).toBe("def foo(): pass");
    expect(argsOf(toolCalls[0]).new).toBe("def foo(): return 42");
  });

  it("грейсфул при обрезке тела (нет @@end@@)", () => {
    const text =
      "```tool_call\n" +
      '{"name":"write_file","arguments":{"path":"t.txt","content":"@@content@@"}}\n```\n' +
      "@@content@@\nстрока 1\nстрока 2 без закрытия";
    const { toolCalls } = parseToolCalls(text);
    expect(argsOf(toolCalls[0]).content).toBe("строка 1\nстрока 2 без закрытия");
  });

  it("толерантный JSON: типографские кавычки + висячая запятая", () => {
    const text = "```tool_call\n{«name»: «read_file», «arguments»: {«path»: «app.py»,}}\n```";
    const { toolCalls } = parseToolCalls(text);
    expect(toolCalls.length).toBe(1);
    expect(toolCalls[0].function.name).toBe("read_file");
  });

  it("grep обычным JSON", () => {
    const { toolCalls } = parseToolCalls('```tool_call\n{"name":"grep","arguments":{"pattern":"foo"}}\n```');
    expect(argsOf(toolCalls[0]).pattern).toBe("foo");
  });

  it("обратная совместимость: тело прямо в JSON", () => {
    const { toolCalls } = parseToolCalls(
      '```tool_call\n{"name":"write_file","arguments":{"path":"b.py","content":"x=1\\ny=2"}}\n```',
    );
    expect(argsOf(toolCalls[0]).content).toBe("x=1\ny=2");
  });

  it("```python — не вызов", () => {
    const { clean, toolCalls } = parseToolCalls("Пример:\n```python\nimport os\nprint(1)\n```");
    expect(toolCalls.length).toBe(0);
    expect(clean).toContain("import os");
  });

  it("@@end@@ в середине строки не обрывает тело", () => {
    const text =
      "```tool_call\n" +
      '{"name":"write_file","arguments":{"path":"c.txt","content":"@@content@@"}}\n```\n' +
      '@@content@@\nprint("@@end@@ в середине строки")\nреальный конец\n@@end@@\n';
    const { toolCalls } = parseToolCalls(text);
    expect(argsOf(toolCalls[0]).content).toContain("в середине строки");
    expect(argsOf(toolCalls[0]).content.endsWith("реальный конец")).toBe(true);
  });

  it("обёртка ```python снимается с тела", () => {
    const text =
      "```tool_call\n" +
      '{"name":"write_file","arguments":{"path":"g.py","content":"@@content@@"}}\n```\n' +
      "@@content@@\n```python\ndef greet():\n    print('hi')\n```\n@@end@@\n";
    const { toolCalls } = parseToolCalls(text);
    expect(argsOf(toolCalls[0]).content).toBe("def greet():\n    print('hi')");
  });

  it("коллизия тел: два write_file -> каждому своё тело по порядку", () => {
    const text =
      "```tool_call\n" +
      '{"name":"write_file","arguments":{"path":"a.py","content":"@@content@@"}}\n```\n' +
      "```tool_call\n" +
      '{"name":"write_file","arguments":{"path":"b.py","content":"@@content@@"}}\n```\n' +
      "@@content@@\nТЕЛО A\n@@end@@\n@@content@@\nТЕЛО B\n@@end@@\n";
    const { toolCalls } = parseToolCalls(text);
    expect(toolCalls.length).toBe(2);
    expect(argsOf(toolCalls[0]).content).toBe("ТЕЛО A");
    expect(argsOf(toolCalls[1]).content).toBe("ТЕЛО B");
  });
});

describe("abbrevArgs + render (B3)", () => {
  const big = "x".repeat(2000);

  it("большой аргумент сворачивается", () => {
    const ab = abbrevArgs(JSON.stringify({ path: "app.py", content: big }));
    expect(ab).toContain("content=<2000 символов>");
    expect(ab).not.toContain(big);
    expect(ab).toContain("path=app.py");
  });

  it("история не раздувается + заголовок результата с именем", () => {
    const messages = [
      { role: "system", content: "sys" },
      { role: "user", content: "сделай файл" },
      {
        role: "assistant",
        content: null,
        tool_calls: [
          { id: "c1", type: "function" as const,
            function: { name: "write_file", arguments: JSON.stringify({ path: "app.py", content: big }) } },
        ],
      },
      { role: "tool", tool_call_id: "c1", content: "Записано: app.py (2000 символов)" },
    ];
    const r = renderMessagesToPrompt(messages, null);
    expect(r).not.toContain(big);
    expect(r).toContain("write_file(path=app.py, content=<2000 символов>)");
    expect(r).toContain("[Результат: write_file]");
  });
});
