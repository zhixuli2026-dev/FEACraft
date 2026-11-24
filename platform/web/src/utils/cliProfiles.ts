function bashWord(value: string): string {
  if (/[\0\r\n]/.test(value)) throw new Error("CLI 配置包含无效换行，请检查运行环境配置。");
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

export function commandForCliAgent(name: string, profile: string, env: Record<string, string> = {},
                                   executable?: string, shell = "cmd"): string {
  if (!/^(codex|claude|opencode|kimi)$/.test(name)) throw new Error("不支持的 CLI 工具。");
  if (profile === "codex-wsl" && name === "codex") {
    const assignments = Object.entries(env).map(([key, value]) => {
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new Error("CLI 环境变量名称无效，请检查运行环境配置。");
      return `${key}=${bashWord(value)}`;
    }).join(" ");
    return assignments ? `env ${assignments} codex` : "codex";
  }
  // Host discoveries describe Windows installs. Native WSL/sandbox terminals
  // continue resolving their own installed CLI; never inject a host path there.
  if (shell !== "cmd") return executable?.startsWith("/") ? bashWord(executable) : name;
  if (!executable) return name;
  if (/[\0\r\n"%!]/.test(executable)) {
    throw new Error("CLI 安装路径包含命令提示符不支持的字符，请在运行环境配置中选择其他安装路径。");
  }
  return `"${executable}"`;
}
