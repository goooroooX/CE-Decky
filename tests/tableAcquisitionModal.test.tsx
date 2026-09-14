import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  cancelTableAcquisition: vi.fn(),
  completeTableAcquisition: vi.fn(),
  pollTableAcquisition: vi.fn(),
}));

vi.mock("../src/api", () => api);
vi.mock("@decky/api", () => ({ toaster: { toast: vi.fn() } }));
vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  DialogButton: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  DropdownItem: ({ label, rgOptions, selectedOption, onChange, disabled }: any) => <label>{label}<select aria-label={label} disabled={disabled} value={selectedOption} onChange={(event) => onChange({ data: event.target.value })}>{rgOptions.map((item: any) => <option key={item.data} value={item.data}>{item.label}</option>)}</select></label>,
  Field: ({ label, description, children }: any) => <div><span>{label}</span><span>{description}</span>{children}</div>,
  Focusable: ({ children }: any) => <div>{children}</div>,
  ModalRoot: ({ children, onCancel }: any) => <div><button aria-label="Controller Back" onClick={onCancel}>Back</button>{children}</div>,
  PanelSection: ({ title, spinner, children }: any) => <section aria-label={title}><header data-testid="section-header">{title}{spinner ? <span role="progressbar">Loading</span> : null}</header>{children}</section>,
  PanelSectionRow: ({ children }: any) => <div>{children}</div>,
  Spinner: () => <span role="progressbar">Loading</span>,
  TextField: (props: any) => <label>{props.label}<input aria-label={props["aria-label"] ?? props.label} placeholder={props.placeholder} value={props.value} disabled={props.disabled} onChange={props.onChange} /></label>,
  gamepadDialogClasses: {
    Field: "Field", FieldLabel: "FieldLabel", FieldDescription: "FieldDescription",
    FieldLeftColumn: "FieldLeftColumn", CompactPadding: "CompactPadding",
    WithBottomSeparatorStandard: "WithBottomSeparatorStandard",
    WithBottomSeparatorThick: "WithBottomSeparatorThick",
  },
}));

import { TableAcquisitionModal } from "../src/modals/TableAcquisitionModal";

const SCOPE = "game-10\u0000example game\u0000";
import { forgetAllRejectedArtifacts, isRejectedArtifact } from "../src/tableImport";

const waiting = {
  acquisition_id: "a".repeat(32), provider: "playground", artifact_id: "page-1:file-2",
  filename: "Example.CT", state: "waiting_provider", error: null, bytes_received: 0,
  expected_bytes: 100, provider_wait_seconds: 60, source_page: null, inspection: null,
  imported: null, execution_consent: null,
};

import { canAutoImportLocalMember, canAutoImportTableMember, forgetRejectedArtifact, forgetRejectedArtifacts, rememberRejectedArtifact } from "../src/tableImport";

describe("damaged-artifact retirement scope", () => {
  beforeEach(() => forgetAllRejectedArtifacts());

  it("keeps one game's retired artifact when another game's search is refreshed", () => {
    // The mark belongs to the exact cached result snapshot that proved the
    // bytes damaged. Clearing it globally on any fresh search made the damaged
    // artifact selectable again in every other game's still-cached results.
    const gameA = "10:steam\u0000game a\u0000";
    const gameB = "11:steam\u0000game b\u0000";
    const damaged = { provider: "playground", artifact_id: "page-1:file-2", artifact_rejected: true } as any;

    expect(rememberRejectedArtifact(gameA, damaged)).toBe(true);
    expect(isRejectedArtifact(gameA, "playground", "page-1:file-2")).toBe(true);
    expect(isRejectedArtifact(gameB, "playground", "page-1:file-2")).toBe(false);

    forgetRejectedArtifacts(gameB);
    expect(isRejectedArtifact(gameA, "playground", "page-1:file-2")).toBe(true);

    forgetRejectedArtifacts(gameA);
    expect(isRejectedArtifact(gameA, "playground", "page-1:file-2")).toBe(false);
  });

  it("frees one retired row without freeing the rest of its own search", () => {
    // The retry press names the rows on the page in front of the user, and one
    // search snapshot spans pages: clearing the whole scope freed rows on every
    // other page from a press that had counted one.
    const scope = "10:steam\u0000game a\u0000";
    const first = { provider: "playground", artifact_id: "page-1:file-2", artifact_rejected: true } as any;
    const second = { provider: "playground", artifact_id: "page-9:file-9", artifact_rejected: true } as any;
    rememberRejectedArtifact(scope, first);
    rememberRejectedArtifact(scope, second);

    forgetRejectedArtifact(scope, "playground", "page-1:file-2");

    expect(isRejectedArtifact(scope, "playground", "page-1:file-2")).toBe(false);
    expect(isRejectedArtifact(scope, "playground", "page-9:file-9")).toBe(true);
    // Forgetting one that was never retired is not an error and frees nothing.
    forgetRejectedArtifact(scope, "playground", "page-4:file-4");
    expect(isRejectedArtifact(scope, "playground", "page-9:file-9")).toBe(true);
  });
});

/** CE Decky renders its own section heading, so Steam's title is not the one. */
function sectionHeading(): HTMLElement {
  const heading = document.querySelector(".ce-decky-heading");
  if (!heading) throw new Error("no section heading rendered");
  return heading as HTMLElement;
}

describe("Playground acquisition modal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    forgetAllRejectedArtifacts();
    vi.useFakeTimers();
    api.cancelTableAcquisition.mockResolvedValue({ ...waiting, state: "cancelled", provider_wait_seconds: null });
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it("owns the provider countdown in a separate cancellable controller modal", async () => {
    api.pollTableAcquisition.mockResolvedValue({ ...waiting, provider_wait_seconds: 59 });
    const onClose = vi.fn();
    render(<TableAcquisitionModal initialStatus={waiting as any} searchScope={SCOPE} onImported={vi.fn()} onClose={onClose} />);
    // The sentence and the number are separate: the number is the only part
    // that changes every second, and it sits on the right of the same block as
    // the file it belongs to rather than at the end of a fixed sentence.
    expect(screen.getByText("Playground is preparing the download")).toBeTruthy();
    expect(screen.getByText("60s remaining")).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    expect(screen.getByText("59s remaining")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel acquisition" }));
    await act(async () => { await Promise.resolve(); });
    expect(api.cancelTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("names the wait as a cooldown rather than as preparing a download", async () => {
    // Two different things wear the same countdown: a provider that promised a
    // link after a wait, and a provider refusing right now. On the target the
    // second one was reported as a failed download, and the user's own answer
    // was to press the row again a minute later, which worked.
    const limited = { ...waiting, provider: "fearless", provider_wait_reason: "rate_limited", provider_wait_seconds: 5 };
    api.pollTableAcquisition.mockResolvedValue(limited);
    render(<TableAcquisitionModal initialStatus={limited as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByText("Waiting out the source's download cooldown")).toBeTruthy();
    expect(screen.getByText("retrying in 5s")).toBeTruthy();
    expect(screen.queryByText(/preparing the download/)).toBeNull();
  });

  it("names the provider the artifact is actually coming from", async () => {
    // The screen was titled after Playground whatever provider was serving it,
    // and on this device every download of the session came from FearLess.
    const fromFearless = { ...waiting, provider: "fearless" };
    api.pollTableAcquisition.mockResolvedValue(fromFearless);
    render(<TableAcquisitionModal initialStatus={fromFearless as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);

    expect(sectionHeading().textContent).toContain("Download table from FearLess");
  });

  it("hands focus to the action that matters once the download settles", async () => {
    // Steam repaints the focus ring when input moves it. A row focused while
    // the download ran changes underneath it when the download ends - the
    // countdown and the spinner go, a failure appears below - and the ring
    // stayed where it was until the user pushed the stick, which reads as a
    // dialog that broke rather than one that finished.
    const failed = { ...waiting, state: "failed", provider_wait_seconds: null, error: "no route" };
    api.pollTableAcquisition.mockResolvedValue(failed);
    render(<TableAcquisitionModal initialStatus={waiting as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));
  });

  it("waits under the countdown in the block that names the file, not in a row of its own", async () => {
    // A spinner in its own row under the header pushed every control below it
    // down for the length of the download and then let them jump back up. The
    // two things that say this is still running belong together, beside the
    // file they are both about.
    api.pollTableAcquisition.mockResolvedValue({ ...waiting, provider_wait_seconds: 59 });
    render(<TableAcquisitionModal initialStatus={waiting as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);
    const spinners = screen.getAllByRole("progressbar");
    expect(spinners).toHaveLength(1);
    expect(sectionHeading().contains(spinners[0])).toBe(false);
    const countdown = screen.getByText("60s remaining");
    expect(countdown.parentElement?.contains(spinners[0])).toBe(true);
  });

  it("stops waiting in the header once the download is no longer running", async () => {
    render(<TableAcquisitionModal
      initialStatus={{ ...waiting, state: "failed", provider_wait_seconds: null, error: "no route" } as any}
      searchScope={SCOPE}
      onImported={vi.fn()}
      onClose={vi.fn()}
    />);
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("automatically imports an unambiguous downloaded CT before closing", async () => {
    vi.useRealTimers();
    const ready = {
      ...waiting, state: "ready_to_import", provider_wait_seconds: null, bytes_received: 100,
      inspection: { format: "ct", members: [{ path: "Example.CT", size: 100, packed_size: null, encrypted: false, format: "ct" }] },
    };
    api.pollTableAcquisition.mockResolvedValue(ready);
    api.completeTableAcquisition.mockResolvedValue({
      ...ready, state: "imported", inspection: null,
      imported: { sha256: "b".repeat(64), filename: "Example.CT" }, execution_consent: false,
    });
    const onImported = vi.fn().mockResolvedValue(undefined);
    const onClose = vi.fn();
    render(<TableAcquisitionModal initialStatus={waiting as any} searchScope={SCOPE} onImported={onImported} onClose={onClose} />);
    await waitFor(() => expect(api.completeTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id, null, null, null));
    expect(onImported).toHaveBeenCalledWith("b".repeat(64));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the rare archive member and password choice in the acquisition modal", async () => {
    vi.useRealTimers();
    const needsSelection = {
      ...waiting, state: "needs_selection", provider_wait_seconds: null, filename: "Pack.zip",
      inspection: { format: "zip", members: [
        { path: "plain.CT", size: 10, packed_size: 8, encrypted: false, format: "zip" },
        { path: "locked.CT", size: 20, packed_size: 12, encrypted: true, format: "zip" },
      ] },
    };
    api.completeTableAcquisition
      // The provider's own password does not fit this member, so the backend
      // comes back asking for one.
      .mockResolvedValueOnce({ ...needsSelection, error: "archive member requires a password" })
      .mockResolvedValue({
        ...needsSelection, state: "imported", inspection: null,
        imported: { sha256: "c".repeat(64), filename: "locked.CT" }, execution_consent: false,
      });
    const onImported = vi.fn().mockResolvedValue(undefined);
    render(<TableAcquisitionModal initialStatus={needsSelection as any} searchScope={SCOPE} onImported={onImported} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Table in downloaded archive"), { target: { value: "locked.CT" } });

    // The first attempt is deliberately password-less, so the backend can try
    // the password the provider published beside this exact artifact.
    expect(screen.getByLabelText("Archive password (optional; not stored)")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Import selected table" }));
    await waitFor(() => expect(api.completeTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id, null, "locked.CT", null));

    // Only once that failed does the user have to know the password themselves.
    fireEvent.change(await screen.findByLabelText("Archive password (not stored)"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Import selected table" }));
    await waitFor(() => expect(api.completeTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id, null, "locked.CT", "secret"));
    expect(onImported).toHaveBeenCalledWith("c".repeat(64));
  });

  it("tries the provider's own password before asking for one, for a single encrypted member", async () => {
    // The backend reports any encrypted member as `needs_selection`, including
    // an archive with nothing to select. Nothing ran there, and the manual row
    // stays hidden until the hint has been spent, so the download dead-ended
    // with no field, no button and no attempt.
    vi.useRealTimers();
    const locked = {
      ...waiting, state: "needs_selection", provider_wait_seconds: null, filename: "Locked.zip", error: null,
      inspection: { format: "zip", members: [
        { path: "locked.CT", size: 20, packed_size: 12, encrypted: true, format: "zip" },
      ] },
    };
    api.completeTableAcquisition.mockResolvedValue({
      ...locked, state: "imported", inspection: null,
      imported: { sha256: "f".repeat(64), filename: "locked.CT" }, execution_consent: false,
    });
    const onImported = vi.fn().mockResolvedValue(undefined);
    render(<TableAcquisitionModal initialStatus={locked as any} searchScope={SCOPE} onImported={onImported} onClose={vi.fn()} />);

    await waitFor(() => expect(api.completeTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id, null, null, null));
    expect(api.completeTableAcquisition).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onImported).toHaveBeenCalledWith("f".repeat(64)));
  });

  it("exposes the password field once the provider's own password did not work", async () => {
    vi.useRealTimers();
    const locked = {
      ...waiting, state: "needs_selection", provider_wait_seconds: null, filename: "Locked.zip", error: null,
      inspection: { format: "zip", members: [
        { path: "locked.CT", size: 20, packed_size: 12, encrypted: true, format: "zip" },
      ] },
    };
    api.completeTableAcquisition
      .mockResolvedValueOnce({ ...locked, error: "archive member requires a password" })
      .mockResolvedValue({
        ...locked, state: "imported", inspection: null,
        imported: { sha256: "f".repeat(64), filename: "locked.CT" }, execution_consent: false,
      });
    render(<TableAcquisitionModal initialStatus={locked as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);

    await waitFor(() => expect(api.completeTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id, null, null, null));
    // Exactly once: the failed attempt must not retry itself in a loop.
    fireEvent.change(await screen.findByLabelText("Archive password (not stored)"), { target: { value: "secret" } });
    expect(api.completeTableAcquisition).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Import selected table" }));
    await waitFor(() => expect(api.completeTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id, null, "locked.CT", "secret"));
  });

  it("prompts for the password of a single encrypted archive member", async () => {
    vi.useRealTimers();
    const needsPassword = {
      ...waiting, state: "needs_selection", provider_wait_seconds: null, filename: "Locked.zip",
      // This is the state the backend returns after its own attempt with the
      // password the provider published failed, which is when the user has to
      // supply one themselves.
      error: "archive member requires a password",
      inspection: { format: "zip", members: [
        { path: "locked.CT", size: 20, packed_size: 12, encrypted: true, format: "zip" },
      ] },
    };
    api.completeTableAcquisition.mockResolvedValue({
      ...needsPassword, state: "imported", inspection: null,
      imported: { sha256: "d".repeat(64), filename: "locked.CT" }, execution_consent: false,
    });
    render(<TableAcquisitionModal initialStatus={needsPassword as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);

    expect(screen.queryByLabelText("Table in downloaded archive")).toBeNull();
    fireEvent.change(screen.getByLabelText("Archive password (not stored)"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Import selected table" }));

    await waitFor(() => expect(api.completeTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id, null, "locked.CT", "secret"));
  });

  it("says why an encrypted 7z member cannot be imported instead of only disabling Import", async () => {
    // Hiding the password field and disabling the button left a multi-member
    // encrypted 7z looking broken, with Cancel as the only valid action and no
    // statement of why - and the backend path that makes the acquisition
    // terminal is unreachable behind that disabled button.
    vi.useRealTimers();
    const needsSelection = {
      ...waiting, state: "needs_selection", provider_wait_seconds: null, filename: "Pack.7z",
      inspection: { format: "7z", members: [
        { path: "one.CT", size: 10, packed_size: 8, encrypted: true, format: "7z" },
        { path: "two.CT", size: 20, packed_size: 12, encrypted: true, format: "7z" },
      ] },
    };
    render(<TableAcquisitionModal initialStatus={needsSelection as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByText("This entry cannot be imported")).toBeTruthy();
    expect(screen.getByText(/does not open password-protected 7z or rar archives/)).toBeTruthy();
    expect(screen.queryByLabelText(/Archive password/)).toBeNull();
    expect((screen.getByRole("button", { name: "Import selected table" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps the supported members of a mixed archive selectable", async () => {
    vi.useRealTimers();
    const needsSelection = {
      ...waiting, state: "needs_selection", provider_wait_seconds: null, filename: "Pack.7z",
      inspection: { format: "7z", members: [
        { path: "locked.CT", size: 10, packed_size: 8, encrypted: true, format: "7z" },
        { path: "plain.CT", size: 20, packed_size: 12, encrypted: false, format: "7z" },
      ] },
    };
    render(<TableAcquisitionModal initialStatus={needsSelection as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByText("This entry cannot be imported")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Table in downloaded archive"), { target: { value: "plain.CT" } });
    expect(screen.queryByText("This entry cannot be imported")).toBeNull();
    expect((screen.getByRole("button", { name: "Import selected table" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("closes acquisition before handing the imported table to Search and Review", async () => {
    vi.useRealTimers();
    const events: string[] = [];
    const ready = {
      ...waiting, state: "ready_to_import", provider_wait_seconds: null,
      inspection: { format: "ct", members: [{ path: "Example.CT", size: 100, packed_size: null, encrypted: false, format: "ct" }] },
    };
    api.pollTableAcquisition.mockResolvedValue(ready);
    api.completeTableAcquisition.mockResolvedValue({
      ...ready, state: "imported", inspection: null,
      imported: { sha256: "e".repeat(64), filename: "Example.CT" }, execution_consent: false,
    });
    render(<TableAcquisitionModal
      initialStatus={waiting as any}
      onClose={() => { events.push("acquisition-close"); }}
      onImported={async () => { events.push("review-handoff"); }}
    />);

    await waitFor(() => expect(events).toEqual(["acquisition-close", "review-handoff"]));
  });

  it("retires a damaged artifact so the catalog stops offering the same bytes", async () => {
    const damaged = {
      ...waiting, state: "failed", provider_wait_seconds: null, artifact_rejected: true,
      error: "the file is not a valid Cheat Engine table: its XML is malformed (mismatched tag: line 393, column 10). The file is damaged at its source; choose a different table.",
    };
    api.pollTableAcquisition.mockResolvedValue(damaged);
    render(<TableAcquisitionModal initialStatus={waiting as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });

    expect(screen.getByText("Acquisition failed")).toBeTruthy();
    expect(screen.getByText(/choose a different table/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close" })).toBeTruthy();
    expect(isRejectedArtifact(SCOPE, "playground", "page-1:file-2")).toBe(true);
  });

  it("records an artifact already retired before the modal opened", async () => {
    const damaged = {
      ...waiting, state: "failed", provider_wait_seconds: null, artifact_rejected: true,
      error: "the file is not a valid Cheat Engine table: its XML is malformed (mismatched tag: line 1, column 1). The file is damaged at its source; choose a different table.",
    };
    render(<TableAcquisitionModal initialStatus={damaged as any} searchScope={SCOPE} onImported={vi.fn()} onClose={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });

    expect(api.pollTableAcquisition).not.toHaveBeenCalled();
    expect(isRejectedArtifact(SCOPE, "playground", "page-1:file-2")).toBe(true);
  });

  it("cleans backend staging before closing a failed acquisition", async () => {
    vi.useRealTimers();
    const failed = { ...waiting, state: "failed", provider_wait_seconds: null, error: "invalid table" };
    api.cancelTableAcquisition.mockResolvedValue(failed);
    const onClose = vi.fn();
    render(<TableAcquisitionModal initialStatus={failed as any} searchScope={SCOPE} onImported={vi.fn()} onClose={onClose} />);

    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(() => expect(api.cancelTableAcquisition).toHaveBeenCalledWith(waiting.acquisition_id));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("local archive import rule", () => {
  it("auto-imports one plain member and never one that is encrypted", () => {
    // A file the user picked from their own device has no provider password
    // hint, so the one password-less attempt that rule allows can only fail:
    // reusing it here meant one encrypted ZIP could never reach its password
    // prompt and one encrypted 7z never reached the explanation of why it
    // cannot be used at all.
    const plain = [{ path: "Game.CT", size: 10, packed_size: null, encrypted: false, format: "zip" }] as any;
    const locked = [{ path: "Game.CT", size: 10, packed_size: null, encrypted: true, format: "zip" }] as any;
    const two = [...plain, { path: "Other.CT", size: 10, packed_size: null, encrypted: false, format: "zip" }] as any;

    expect(canAutoImportLocalMember(plain)).toBe(true);
    expect(canAutoImportLocalMember(locked)).toBe(false);
    expect(canAutoImportLocalMember(two)).toBe(false);
    expect(canAutoImportLocalMember([])).toBe(false);

    // The provider rule is deliberately different and stays that way: there the
    // backend may hold a password published beside that exact artifact.
    expect(canAutoImportTableMember(locked)).toBe(true);
    expect(canAutoImportTableMember(locked, true)).toBe(false);
  });
});
