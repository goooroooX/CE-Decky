import type { AcquisitionStatus, ArchiveMember } from "./types";

/**
 * Whether these served bytes could hold more than one table.
 *
 * The backend accepts exactly `.CT`, `.zip` and `.7z`/`.7zip` sources, so
 * anything that is not a direct `.CT` is treated as an archive here - an
 * unrecognized extension is the conservative case, not the permissive one.
 */
export function isArchiveFilename(filename: string): boolean {
  return !/\.ct$/i.test(filename.trim());
}

/**
 * Whether the one member can be imported without asking the user anything yet.
 *
 * An encrypted single member qualifies for exactly one attempt: the provider
 * often publishes the archive password in the public text beside the file and
 * the backend already tries that hint when no password is supplied, so
 * demanding one up front dead-ended a completed download at a field the user
 * had no way to fill. If the hint does not work, the attempt fails with a
 * password error and the prompt is what comes next.
 */
/**
 * Whether this member can never be imported however the user answers.
 *
 * Encrypted 7z is refused on purpose: the external `7z` tool would take the
 * password on its process argv, so CE Decky does not transport one. Inspection
 * still reports the member, and both controller paths were treating it like a
 * supported password workflow - asking for a password no entry can satisfy.
 */
/** Archive kinds only 7-Zip opens, which takes a password on its argv alone. */
const ARGV_PASSWORD_FORMATS = new Set(["7z", "rar"]);

export function isUnsupportedEncryptedMember(member: ArchiveMember | null | undefined): boolean {
  return Boolean(member?.encrypted && ARGV_PASSWORD_FORMATS.has(member.format));
}

/**
 * The one explanation every surface that can select such a member shows.
 *
 * Hiding the password field and disabling the action was not enough on the
 * provider paths: with no explanation the archive simply looked broken, and
 * because the action was disabled the backend path that makes the acquisition
 * terminal was unreachable, so it sat in `needs_selection` with Cancel as the
 * only valid move.
 */
export const UNSUPPORTED_MEMBER_EXPLANATION =
  "CE Decky does not open password-protected 7z or rar archives, because the password would have "
  + "to be passed to another program on its command line. Re-pack the table as a zip, or open the "
  + ".CT directly.";

/**
 * Whether the user now has to supply the archive password themselves.
 *
 * The backend tries the password the provider published beside the artifact
 * when none is given, so the first attempt is deliberately password-less. An
 * acquisition that comes back asking about a password has already spent it -
 * which is also true for a state restored from the backend rather than
 * observed here, so the answer is derived from the reported error as well.
 */
export function passwordPromptRequired(status: AcquisitionStatus, hintTried: boolean): boolean {
  return hintTried || /password/i.test(status.error ?? "");
}

export function canAutoImportTableMember(
  members: readonly ArchiveMember[],
  hintAlreadyTried = false,
): boolean {
  if (members.length !== 1) return false;
  return !members[0].encrypted || !hintAlreadyTried;
}

/**
 * Whether a file the user opened themselves can be imported without asking.
 *
 * Deliberately not the rule above with its default argument. That rule allows
 * one password-less attempt at a single encrypted member because the backend
 * may hold a password the provider published beside that exact artifact - and a
 * file the user picked from their own device has no such hint, so the attempt
 * can only fail. Reusing it there imported with no password and reported the
 * refusal, which meant one encrypted ZIP could never reach its password prompt
 * and one encrypted 7z never reached the explanation of why it cannot be used.
 */
export function canAutoImportLocalMember(members: readonly ArchiveMember[]): boolean {
  return members.length === 1 && !members[0].encrypted;
}

/**
 * Artifacts whose exact bytes are a damaged table. Retrying one costs another
 * provider countdown and can never succeed, so the catalog stops offering it
 * for as long as the search results it came from stay cached.
 *
 * The mark belongs to the exact result snapshot that proved the bytes bad, not
 * to the session. A single global set meant refreshing one game's search
 * cleared the marks of every other game whose cached results were untouched,
 * so switching back to that game offered the damaged artifact again and the
 * user paid another countdown and download for bytes already known unusable.
 */
const rejectedArtifacts = new Map<string, Set<string>>();

function artifactKey(provider: string, artifactId: string): string {
  return `${provider}:${artifactId}`;
}

/** Returns true when this outcome newly retires the artifact for that search. */
export function rememberRejectedArtifact(searchScope: string, status: AcquisitionStatus): boolean {
  if (!status.artifact_rejected) return false;
  const key = artifactKey(status.provider, status.artifact_id);
  let scoped = rejectedArtifacts.get(searchScope);
  if (!scoped) {
    scoped = new Set<string>();
    rejectedArtifacts.set(searchScope, scoped);
  }
  if (scoped.has(key)) return false;
  scoped.add(key);
  return true;
}

export function isRejectedArtifact(searchScope: string, provider: string, artifactId: string): boolean {
  return rejectedArtifacts.get(searchScope)?.has(artifactKey(provider, artifactId)) ?? false;
}

/**
 * Forget the artifacts retired for one exact search snapshot. A provider can
 * replace a damaged upload with a fixed file under the same identity, so the
 * search that is actually being re-read must be able to offer it again - and
 * only that one.
 */
export function forgetRejectedArtifacts(searchScope: string): void {
  rejectedArtifacts.delete(searchScope);
}

/**
 * Forget exactly one retired artifact within a search snapshot.
 *
 * The retry press acts on the rows a user can see, and a search snapshot spans
 * pages: clearing the whole scope from a press that named one row freed rows on
 * every other page too, which is the same overreach that made the durable half
 * of this count rows nobody was looking at.
 */
export function forgetRejectedArtifact(searchScope: string, provider: string, artifactId: string): void {
  const scoped = rejectedArtifacts.get(searchScope);
  if (!scoped) return;
  scoped.delete(artifactKey(provider, artifactId));
  if (scoped.size === 0) rejectedArtifacts.delete(searchScope);
}

/** Drop every retirement mark. Test isolation only. */
export function forgetAllRejectedArtifacts(): void {
  rejectedArtifacts.clear();
}
