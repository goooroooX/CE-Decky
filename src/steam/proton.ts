export interface AppCompatibilitySnapshot {
  appId: number;
  isShortcut: boolean;
  compatToolName: string;
  compatToolDisplayName: string;
  compatToolPriority: number;
  platforms: readonly string[];
}

export type ProtonEligibility =
  | { state: "confirmed_proton"; reason: string; tool: string }
  | { state: "compatibility_required"; reason: string }
  | { state: "native_linux"; reason: string }
  | { state: "uncertain"; reason: string };

function looksLikeProton(name: string): boolean {
  const value = name.trim().toLocaleLowerCase();
  return /^(?:proton(?:$|[_\s-])|ge(?:[_\s-]?proton)(?:$|[_\s-]?\d)|proton(?:[_\s-]?ge)(?:$|[_\s-]?\d))/.test(value);
}

export function classifyProton(snapshot: AppCompatibilitySnapshot): ProtonEligibility {
  const toolName = snapshot.compatToolName.trim();
  const toolDisplay = snapshot.compatToolDisplayName.trim();
  if (toolName || toolDisplay) {
    if (looksLikeProton(toolName) || looksLikeProton(toolDisplay)) {
      return { state: "confirmed_proton", reason: "Steam reports a Proton compatibility tool", tool: toolDisplay || toolName };
    }
    return { state: "uncertain", reason: `Steam reports a non-Proton or unrecognized compatibility tool: ${toolDisplay || toolName}` };
  }

  const platforms = new Set(snapshot.platforms.map(value => value.trim().toLocaleLowerCase()).filter(Boolean));
  const windows = platforms.has("windows");
  const linux = platforms.has("linux");

  if (snapshot.isShortcut) {
    return { state: "uncertain", reason: "non-Steam shortcut has no explicit Proton identity" };
  }
  if (windows && !linux) {
    return { state: "compatibility_required", reason: "Windows-only Steam app requires a compatibility route on SteamOS, but the exact Proton identity is unresolved" };
  }
  if (linux && !windows) {
    return { state: "native_linux", reason: "Linux-only app with no forced compatibility tool" };
  }
  if (windows && linux) {
    return { state: "uncertain", reason: "dual-platform app with default compatibility selection" };
  }
  return { state: "uncertain", reason: "platform and compatibility metadata are insufficient" };
}
