import { useUiSurface } from "../useUiSurface";
import { traceUiAction, traceUiEdit, startUiOperation } from "../uiActions";
import { DialogButton, Dropdown, Field, Focusable, ModalRoot, PanelSection, PanelSectionRow, Spinner, TextField } from "@decky/ui";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ModalActions, modalActionStyle } from "../components/ModalActions";
import { antiCheatBlockedReason, defaultTargetProcess, installedGameExecutables, isValidProcessBasename, isWineRuntimeExecutable, launchExecutableBasename, scriptDefaultsOn, withoutWineRuntimeProcesses } from "../uiModel";
import type { GameExecutableListing, TableInspection, TableStatus } from "../types";
import { describeError } from "../errors";
import { logUiFailure } from "../supportLog";
import { ActionGroup, BELOW_FIELD_CLASS, CONTENTS_ONLY, DensePanel, InfoFields, PanelRow, SectionHeading, SmallButton, focusFirstEnabled } from "../components/PanelDensity";
import { TableCodeModal } from "./TableCodeModal";

const CUSTOM_PROCESS = "__custom_process__";

interface Props {
  table: TableStatus;
  inspection: TableInspection;
  /** Windows .exe basenames observed running for this game, newest observation first. */
  observedProcesses?: readonly string[];
  /** The game's own launch executable, which is commonly a launcher rather than the game. */
  launchExecutable?: string | null;
  /**
   * The Windows executables in this game's own installed folder.
   *
   * The weakest evidence there is and the only kind that exists before the game
   * has ever been started, which is the case this screen used to have no answer
   * for: a table that names no process, for a Steam game that is not running,
   * left the picker empty and **Use this table** could not be pressed at all
   * until the game had been launched once. Offered last, marked as what it is,
   * and never presented as a statement that any of them owns the game's memory.
   */
  installedExecutables?: GameExecutableListing | null;
  initialTargetProcess?: string | null;
  /**
   * Look at the game's processes again, for a game started while this is open.
   *
   * The snapshot is taken once, before this screen opens, and the empty state
   * tells the user to start the game - which used to change nothing here,
   * because a modal is a detached tree that never receives new props.
   */
  onRefreshProcesses?: () => Promise<readonly string[]>;
  /**
   * Activate the table. `report` names the step being run so this screen can
   * say what it is waiting for: starting Cheat Engine and waiting for its
   * resident bridge is minutes rather than moments, and a dialog that only goes
   * grey for that long is indistinguishable from one that has hung.
   */
  onUse: (targetProcess: string, report: (step: string, stoppable?: boolean) => void) => Promise<void> | void;
  /**
   * Stop the Cheat Engine the activation owns. Offered only while a step has
   * reported that it owns one: the durable writes that run first cannot be
   * taken back, and before the first launch there is nothing running to stop.
   * Resolves to a sentence for this screen when there is nothing to stop.
   */
  onAbort?: () => Promise<string | null>;
  onCancel: () => void;
}

/** `m:ss` since the press, for a wait that is long enough to be doubted. */
function elapsedText(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/**
 * The process control and the press that fills it again.
 *
 * A game started while this screen is open changes nothing here otherwise: the
 * snapshot is taken before it opens and a modal never receives new props, so
 * looking again is a real press rather than a refresh. It is still a secondary
 * one, and it had a full-width row of its own directly under a control that
 * already takes the width, which on a handheld is a whole row of the display
 * spent on the smaller half of one decision.
 *
 * The compact secondary action stays beside the process control on every screen.
 */
function ProcessChoice(
  { children, label, description, onRescan, rescanning, disabled }:
  {
    children: ReactNode;
    label: ReactNode;
    description?: ReactNode;
    onRescan: (() => void) | null;
    rescanning: boolean;
    disabled: boolean;
  },
) {
  return (
    // The same class the game picker's own stacked selector carries, because it
    // is the same shape and needs the same thing: this control is drawn under
    // its label rather than beside it, so the block it opens is as wide as the
    // row and lands directly below it. Without the air it sits flat on the row
    // explaining where the name came from, and the explanation reads as part of
    // the control.
    <div className={BELOW_FIELD_CLASS}>
      <PanelSectionRow>
        {/* One `Field` for the whole row, holding the control and the press
            beside it, rather than a `DropdownItem` with the press outside it.
            Steam's own `Field` is what paints a row's ground, so a row whose
            field was only as wide as its dropdown had a ground that stopped
            there: on a 4K television the block ended at four fifths of the
            window and Refresh stood in the gap beside it, on its own. Nothing
            else on this screen is drawn that way.

            One group rather than two boxes on one line: side by side, Steam
            treats siblings in a plain box as separate steps, so the stick went
            down from the control to the button instead of across to it.
            `ActionGroup` is the only thing here that builds such a group, and it
            keeps the rule that a lone enabled control is never wrapped in one -
            which is what this row is when there is nothing to refresh. */}
        <Field label={label} description={description} childrenLayout="below" childrenContainerWidth="max" bottomSeparator="standard">
          <ActionGroup style={{ gap: 8 }}>
            <div style={{ flex: "1 1 0", minWidth: 0 }}>{children}</div>
            {onRescan === null ? null : (
              <SmallButton size="medium" disabled={disabled || rescanning} onClick={traceUiAction("table_review_modal.on_rescan_2", onRescan)}>
                {rescanning ? "Looking…" : "Refresh"}
              </SmallButton>
            )}
          </ActionGroup>
        </Field>
      </PanelSectionRow>
    </div>
  );
}

export function TableReviewModal({ table, inspection, observedProcesses: initialObservedProcesses = [], launchExecutable, installedExecutables = null, initialTargetProcess, onRefreshProcesses, onUse, onAbort, onCancel }: Props) {
  useUiSurface("TableReviewModal", table.sha256);
  // Seeded from the snapshot this screen was opened with, and replaced when the
  // user asks again after starting the game.
  const [observedProcesses, setObservedProcesses] = useState<readonly string[]>(initialObservedProcesses);
  const [rescanning, setRescanning] = useState(false);
  // Why the last look for the game's processes failed, and nothing once one
  // has worked. The list beside it is the previous answer, kept on purpose.
  const [rescanError, setRescanError] = useState<string | null>(null);
  // Reading the table is a sub-screen of the decision, not a step in it.
  const [codeOpen, setCodeOpen] = useState(false);
  // Where the ring goes when the reader comes back from it.
  //
  // A sub-screen replaces this tree, so returning mounts the review again and
  // Steam's initial focus lands wherever it would on a screen just opened,
  // which is not the control the user pressed to leave. The press that opened
  // the code is what they are still in the middle of, so the ring goes back on
  // it. Recorded where the screen is actually opened rather than at the press,
  // so nothing is left behind to be spent on some later return.
  const lookInsideRef = useRef<HTMLDivElement | null>(null);
  const returningFromCodeRef = useRef(false);
  const openCode = () => {
    returningFromCodeRef.current = true;
    setCodeOpen(true);
  };
  useEffect(() => {
    if (codeOpen || !returningFromCodeRef.current) return;
    returningFromCodeRef.current = false;
    focusFirstEnabled(lookInsideRef);
  }, [codeOpen]);
  const rescan = () => {
    if (!onRefreshProcesses || rescanning) return;
    setRescanning(true);
    setRescanError(null);
    void onRefreshProcesses()
      .then((next) => {
        setObservedProcesses(next);
        setRescanError(null);
      })
      // The snapshot already on screen stays: a look that failed is not a
      // statement that the game stopped running. It is also not a look that
      // succeeded, and this screen is where the user picks which executable
      // Cheat Engine will attach to, so a press that changed nothing and said
      // nothing left them choosing from a list they had just been given a
      // reason to distrust. The parent records the failure, but its error row
      // is on a panel this screen is drawn over.
      .catch((cause) => {
        logUiFailure("review.process_refresh_failed", cause, { table_sha: table.sha256.slice(0, 12) });
        setRescanError(describeError(cause));
      })
      .finally(() => setRescanning(false));
  };
  const antiCheatReason = antiCheatBlockedReason(observedProcesses);
  // Everything this screen has to say about the table itself, in one block.
  // Each item is at most a sentence, and the block is absent when there is
  // nothing: a healthy table's Review is the screen it always was.
  const defaults = scriptDefaultsOn(inspection);
  const findings = [
    // The signature first: it is the one finding that predicts the table will
    // not open at all. Stated as the measurement it is, with its subject named,
    // rather than as a prediction about this exact file: nothing here can tell
    // Cheat Engine's three refusals apart, and one of them is a Windows call
    // under Wine rather than anything about the table. The measurement and the
    // helper that took it are in `docs/FIELD_NOTES.md` section 2.
    inspection.has_signature
      ? "This table is signed. Every one of the 16 signed tables tested on this device was refused by Cheat Engine, which says nothing about why."
      : null,
    defaults
      ? `Of this table's ${defaults.switches} on/off cheats, ${defaults.on} are switched on by the table itself. CE Decky turns on only the ones you choose.`
      : null,
  ].filter((item): item is string => item !== null);
  // Most tables never name a process, and the library entry usually points at a
  // launcher rather than the executable that owns the game's memory. Offering
  // what the game is actually running keeps this a controller choice instead of
  // an .exe basename the user has to know and type blind.
  // A Proton prefix runs Wine's own services, Proton's helpers and the game's
  // crash reporter beside the game, so the raw observation is mostly noise. The
  // table's own hints are never filtered: they are the author's intent.
  const observedGameProcesses = useMemo(
    () => withoutWineRuntimeProcesses(observedProcesses),
    [observedProcesses],
  );
  const candidates = useMemo(() => {
    const merged: string[] = [];
    const seen = new Map<string, number>();
    const add = (candidate: string, preferSpelling: boolean) => {
      if (!isValidProcessBasename(candidate)) return;
      const key = candidate.toLowerCase();
      const existing = seen.get(key);
      if (existing !== undefined) {
        // Windows process matching is case-insensitive. When the table and the
        // live process disagree only in spelling, display and submit the exact
        // spelling observed from the running game.
        if (preferSpelling) merged[existing] = candidate;
        return;
      }
      seen.set(key, merged.length);
      merged.push(candidate);
    };
    for (const candidate of inspection.process_candidates) add(candidate, false);
    for (const candidate of observedGameProcesses) add(candidate, true);
    // Many games start through a launcher of their own, which Steam records as
    // the shortcut's target and Proton reports as the launch target. It is
    // never the default - the table's hint and the running game outrank it -
    // but it is the one other executable known to belong to this game, and a
    // table whose cheats live in the launcher process had no way to reach it
    // without the user typing the name blind. Store launchers and Wine's own
    // programs stay out: those belong to no game.
    if (launchExecutable && !isWineRuntimeExecutable(launchExecutable)) {
      add(launchExecutableBasename(launchExecutable), false);
    }
    // Last, because nothing here has ever been seen running. What Steam itself
    // starts for this game comes in the order Steam lists it; where Steam had
    // nothing to say, the walk of the game's folder is shallowest first, which
    // is not a guess about names - Half-Life 2's root holds exactly `hl2.exe`
    // while its `bin/` holds thirty-odd SDK compilers that read like plausible
    // programs and are none of them the game.
    for (const installed of installedGameExecutables(installedExecutables)) add(installed.name, false);
    return merged;
  }, [inspection, observedGameProcesses, launchExecutable, installedExecutables]);
  // Which of the offered names are known ONLY from the game's folder, so the
  // row that offers one can say so. Built against the stronger sources rather
  // than from the listing alone: a name the table itself declares and the
  // folder also holds - `hl2.exe` is both - was being labelled as read off a
  // disk and annotated "nothing has been seen running", which presents the
  // table author's own intent as the weakest evidence there is.
  const fromGameFiles = useMemo(() => {
    const stronger = new Set<string>([
      ...inspection.process_candidates.map((name) => name.toLowerCase()),
      ...observedGameProcesses.map((name) => name.toLowerCase()),
      ...(launchExecutable ? [launchExecutableBasename(launchExecutable).toLowerCase()] : []),
    ]);
    return new Set(installedGameExecutables(installedExecutables)
      .map((item) => item.name.toLowerCase())
      .filter((name) => !stronger.has(name)));
  }, [installedExecutables, inspection, observedGameProcesses, launchExecutable]);
  // The names Steam itself declares for this game, which is a different claim
  // from a name a walk of the folder turned up and is said differently.
  const declared = useMemo(
    () => new Set(installedGameExecutables(installedExecutables)
      .filter((item) => item.declared)
      .map((item) => item.name.toLowerCase())),
    [installedExecutables],
  );
  /**
   * Whether this game holds no Windows program at all, proven rather than
   * merely not found.
   *
   * A Steam game installed on Linux is often the native build: a Steam Deck's
   * Half-Life 2 has `hl2.sh` and `hl2_linux` and not one `.exe` anywhere, while
   * Steam's own record for it names `hl2.exe`, which belongs to the Windows
   * depot the other machine has. Cheat Engine attaches to a Windows process
   * under Proton, so there is nothing here for it to attach to at all, and a
   * field asking the reader to type an `.exe` basename is asking them to name
   * something that does not exist.
   *
   * Only the walked answer proves this. A game that is not installed, a library
   * that could not be read and a walk that hit its own bound are each a reason
   * to keep the manual entry, because none of them says there is no Windows
   * program: they say nobody looked.
   *
   * One thing overturns it, and it is not a name: a Windows process actually
   * seen running for this game. That is the device contradicting its own walk,
   * and what is running wins. A name the table declares and a name Steam's
   * launch record carries are neither of them that - they are what the Windows
   * depot would be called, said by a file and by an account, and on this device
   * that program is not there. Counting them let a table hint reading `hl2.exe`
   * hide this warning on a machine whose Half-Life 2 is `hl2_linux`, and leave
   * the reader authorizing a target nothing can ever attach to.
   */
  const observed = useMemo(
    () => new Set(observedGameProcesses.filter(isValidProcessBasename).map((candidate) => candidate.toLowerCase())),
    [observedGameProcesses],
  );
  const noWindowsProgram = installedExecutables?.cause === "no_windows_executable" && observed.size === 0;
  const launchBasename = launchExecutableBasename(launchExecutable).toLowerCase();
  const initial = defaultTargetProcess({
    confirmed: initialTargetProcess,
    tableHints: inspection.process_candidates,
    observed: observedGameProcesses,
    launchExecutable,
    installed: installedExecutables,
  });
  // DropdownItem selects by exact option data, while Windows executable names
  // and our evidence matching are case-insensitive. Resolve the model's choice
  // back to the canonical candidate before initializing the control; otherwise
  // Decky shows the custom-entry row even though the adjacent text field holds
  // the correct process.
  const initialCandidate = candidates.find((candidate) => candidate.toLowerCase() === initial.toLowerCase());
  const [selector, setSelector] = useState(initialCandidate ?? CUSTOM_PROCESS);
  const [customProcess, setCustomProcess] = useState(initialCandidate ? "" : initial);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const abortingRef = useRef(false);
  const abortPendingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [step, setStep] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [aborting, setAborting] = useState(false);
  // Whether the step now running owns a Cheat Engine that stopping would end.
  // The button used to be offered for the whole activation, including the
  // durable profile and consent writes that precede any launch, where pressing
  // it could only answer that there was nothing to stop.
  const [stoppable, setStoppable] = useState(false);
  // The wait is seconds long on the device it was watched on and bounded by the
  // backend's five minute deadline, and its only visible part is this row, so
  // the clock has to run rather than be sampled when something else happens to
  // re-render. It exists only while there is something to time.
  useEffect(() => {
    if (startedAt === null) {
      setElapsedSeconds(0);
      return;
    }
    setElapsedSeconds((Date.now() - startedAt) / 1000);
    const handle = window.setInterval(() => setElapsedSeconds((Date.now() - startedAt) / 1000), 1000);
    return () => window.clearInterval(handle);
  }, [startedAt]);
  // A table is used as written unless it could not be. A label carrying a
  // character that hides itself is the one thing a cheat list can be made to
  // lie with, so the removal is said rather than swallowed.
  const sanitizedLabels = inspection.sanitized_labels ?? 0;
  const droppedValues = inspection.dropped_values ?? 0;
  // A whole list is one picker gone from a record, not one value, and the list
  // this was added for holds 6508 of them: counted among the values it told a
  // user deciding whether to use the table that it had lost one.
  const droppedLists = inspection.dropped_value_lists ?? 0;
  const notTakenAsWritten = [
    sanitizedLabels ? `${sanitizedLabels} label(s) had hidden characters removed` : "",
    droppedValues ? `${droppedValues} value(s) this cannot carry were dropped` : "",
    droppedLists ? `${droppedLists} value list(s) too large to carry were dropped` : "",
  ].filter(Boolean).join(" · ");
  const targetProcess = selector === CUSTOM_PROCESS ? customProcess.trim() : selector;
  const targetValid = isValidProcessBasename(targetProcess);
  /**
   * Whether the decision this window is for can be made right now.
   *
   * A game proven to hold no Windows program is one of the ways it cannot: the
   * screen above says so and offers nothing to choose, but the target a table
   * hint or an older selection left behind is still a valid `.exe` basename, so
   * validity alone kept this press live over a process that does not exist on
   * this device.
   */
  const usable = !busy && !aborting && targetValid && !antiCheatReason && !noWindowsProgram;
  const origin = table.origins[table.origins.length - 1];

  const use = async () => {
    if (!targetValid || antiCheatReason || noWindowsProgram || busyRef.current || abortingRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    setStep("Saving the selected table");
    setStoppable(false);
    setStartedAt(Date.now());
    const operation = startUiOperation("review.use", { table_sha: table.sha256, process: targetProcess });
    try {
      await onUse(targetProcess, (next, nextStoppable) => {
        setStep(next);
        setStoppable(Boolean(nextStoppable));
      });
      operation.completed();
    } catch (cause) {
      operation.failed(cause);
      setError(describeError(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
      // Release only after both this activation and its stop RPC have settled.
      if (!abortPendingRef.current) {
        abortingRef.current = false;
        setAborting(false);
      }
      setStoppable(false);
      setStep(null);
      setStartedAt(null);
    }
  };

  const abort = async () => {
    if (!onAbort || abortingRef.current || !busyRef.current || !stoppable) return;
    abortingRef.current = true;
    abortPendingRef.current = true;
    setAborting(true);
    const operation = startUiOperation("review.abort", { table_sha: table.sha256 });
    let confirmed = false;
    try {
      const nothingToStop = await onAbort();
      confirmed = !nothingToStop;
      // The activation itself ends on its own once the thing it was waiting for
      // is gone, so this reports only the case where nothing could be stopped.
      if (nothingToStop) setError(nothingToStop);
      operation.completed({ nothing_to_stop: Boolean(nothingToStop) });
    } catch (cause) {
      operation.failed(cause);
      setError(describeError(cause));
    } finally {
      abortPendingRef.current = false;
      if (!confirmed || !busyRef.current) {
        abortingRef.current = false;
        setAborting(false);
      }
    }
  };

  // A sub-screen of this one rather than a modal over it: the review is mid
  // decision, its process choice and its progress have to survive being read,
  // and Decky's focus stack is steadier with one root.
  if (codeOpen) {
    return (
      <TableCodeModal
        sha256={table.sha256}
        filename={table.filename}
        onBack={() => setCodeOpen(false)}
      />
    );
  }

  return (
    <ModalRoot onCancel={traceUiAction("table_review_modal.cancel_back", () => { if (!busyRef.current && !abortingRef.current) onCancel(); })}>
      <Focusable style={{ minWidth: 420, maxWidth: 620 }}>
        <DensePanel>
          <PanelSection>
            <SectionHeading>Review cheat table</SectionHeading>
          {/* The four facts this decision is about. They are read, never
              pressed, and on a short screen they are most of the display, so
              there they go two to a line. */}
          <InfoFields items={[
            { label: table.filename, description: `SHA-256 ${table.sha256.slice(0, 12)}… · ${Math.max(1, Math.ceil(table.size / 1024))} KiB` },
            { label: "Source", description: origin ? `${origin.provider} · ${origin.original_filename || table.filename}` : "Local file" },
            { label: "Contents", description: `${inspection.total_entries} entries · ${inspection.controls.length} inspected controls · CE table ${inspection.table_version ?? "unknown"}${notTakenAsWritten ? ` · ${notTakenAsWritten}` : ""}` },
            { label: "Executable content", description: `${inspection.has_lua ? "Lua " : ""}${inspection.has_auto_assembler ? "AutoAssembler " : ""}${inspection.has_forms ? "its own window " : ""}${inspection.embedded_files ? `${inspection.embedded_files} embedded file(s)` : ""}`.trim() || "No static executable-content markers found" },
          ]} />
          {/* The one screen that asks whether this exact SHA may execute what it
              carries, and until now the whole of the answer it offered was the
              count above. Reading the scripts meant Desktop Mode, a file
              manager and a text editor, which a user in Game Mode does not
              have: the question was being asked with no way to answer it. This
              reads the table and runs nothing.

              It belongs beside the sentence that asks the question rather than
              on a full-width row of its own above it: what it does is answer
              that sentence, and a row that carries both is one block instead of
              two. Where there is no such sentence there is nothing to attach it
              to, and it keeps a row. */}
          {table.executable_content ? (
            <PanelSectionRow>
              <PanelRow
                label="Confirmation required"
                description="This exact table SHA can execute Lua, Auto Assembler, embedded content, or a window it brought with it. Using it authorizes only this exact SHA."
                actions={(
                  <div ref={lookInsideRef} style={CONTENTS_ONLY}>
                    <SmallButton size="medium" disabled={busy} onClick={traceUiAction("table_review_modal.look_inside", openCode)}>Look inside</SmallButton>
                  </div>
                )}
              />
            </PanelSectionRow>
          ) : (
            <PanelSectionRow>
              <div ref={lookInsideRef} style={CONTENTS_ONLY}>
                <DialogButton disabled={busy} onClick={traceUiAction("table_review_modal.look_inside_this_table", openCode)}>
                  Look inside this table
                </DialogButton>
              </div>
            </PanelSectionRow>
          )}
          {/* The process choice takes the whole row rather than the column
              left over beside its own description.

              Steam gives an inline control the narrowest column its content
              will accept, and what this one holds is a Windows executable name
              with a note after it: `Grounded2-WinGDK-Shipping.exe · running` in
              a third of a row is a name cut through the middle, on the control
              that decides what Cheat Engine attaches to. Widening the control
              in place would only move that onto the text beside it, because the
              description here is a sentence rather than a caption and says what
              to do when nothing is running. Under the label, both get the row.

              The button below it is full width for the same reason, so the two
              read as one decision and the control that makes it is the wider of
              the two. */}
          {/* On a short screen the control and the button that refills it are
              one block: they are one decision, and the button had a full row of
              its own under a control that already takes the width. Where there
              is room the two stay stacked, which is the shape this screen was
              looked at in. */}
          {/* Said here, where the press is and where the choice is made. The
              panel underneath records it too, and this screen is drawn over
              that panel, so its error row is not something the user can see
              while they are choosing what Cheat Engine attaches to. */}
          {/* One block for everything CE Decky found in this table itself, rather
              than a row per finding: a signed table, a scan that will not match
              and a script that switches itself on are all the same question for
              the reader, which is what they are agreeing to. A table with
              nothing to say renders none of it and this screen looks as it did.
              */}
          {findings.length > 0 && (
            <PanelSectionRow>
              <PanelRow
                testId="review-findings"
                status
                label="What CE Decky found"
                description={findings.join(" ")}
              />
            </PanelSectionRow>
          )}
          {rescanError && (
            <PanelSectionRow>
              <PanelRow
                testId="review-rescan-error"
                status
                label="Could not look for the game's processes"
                description={`${rescanError} The choices below are from the last look that worked.`}
              />
            </PanelSectionRow>
          )}
          {noWindowsProgram && (
            <PanelSectionRow>
              <PanelRow
                status
                testId="review-no-windows-program"
                label="This game is installed as a Linux build"
                description="Its folder holds no Windows program, and Steam's own record names one this device does not have. Cheat Engine attaches to a Windows program running under Proton, so there is nothing here for it to attach to. Install this game's Windows version, by setting a Proton compatibility tool for it in Steam, and open this screen again."
              />
            </PanelSectionRow>
          )}
          {/* No description where there is a list to choose from: the row says
              what it is, the label on each option says what that candidate is,
              and a sentence repeating the obvious is a line of a window that
              does not fit on a handheld. Where there is nothing to choose, the
              description is the whole of what this screen has to say. */}
          {!noWindowsProgram && (
          <ProcessChoice
            label="Game process"
            description={candidates.length
              ? undefined
              : onRefreshProcesses
                ? "This table names no process and none is running for this game. Start the game and press Look again, or enter the .exe basename."
                : "This table names no process and none is running for this game. Start the game and reopen this screen, or enter the .exe basename."
            }
            onRescan={onRefreshProcesses ? () => rescan() : null}
            rescanning={rescanning}
            disabled={busy}
          >
            <Dropdown
              menuLabel="Game process"
              rgOptions={[
                ...candidates.map((candidate) => ({
                  data: candidate,
                  // What each name is, so a choice made before the game has
                  // ever run is made knowing which it is. `running` is the
                  // strongest and `in this game's files` is the weakest: it was
                  // read off the disk and nothing has been seen running.
                  label: observed.has(candidate.toLowerCase())
                    ? `${candidate} \u00b7 running`
                    : candidate.toLowerCase() === launchBasename
                      ? `${candidate} \u00b7 launcher`
                      : declared.has(candidate.toLowerCase())
                        ? `${candidate} \u00b7 Steam starts this`
                        : fromGameFiles.has(candidate.toLowerCase())
                          ? `${candidate} \u00b7 in this game's files`
                          : candidate,
                })),
                { data: CUSTOM_PROCESS, label: "Enter another .exe basename…" },
              ]}
              selectedOption={selector}
              onChange={traceUiAction("table_review_modal.game_process", (option) => {
                const next = String(option.data);
                setSelector(next);
                if (next !== CUSTOM_PROCESS) setCustomProcess("");
              }, (option) => ({ table_sha: table.sha256, process: String(option.data) }))}
              disabled={busy}
            />
          </ProcessChoice>
          )}
          {/* Said where the choice is made, because it is the whole of what
              makes this choice safe to offer: nothing has been observed, this
              is where the name came from, and the first real start is what
              checks it. Only while such a name is actually selected, and each
              of the two says which it is: what Steam starts for this game is a
              statement by the client that has to start it, and a name a walk of
              the folder turned up is not. */}
          {!noWindowsProgram && selector !== CUSTOM_PROCESS && declared.has(selector.toLowerCase()) && (
            <PanelSectionRow>
              <PanelRow
                status
                testId="review-declared-choice"
                label="What Steam starts for this game"
                description="Steam's own record for the version installed here, not an observation."
                help="Nothing has been seen running yet. A game that starts through a launcher of its own declares the launcher, so if it turns out to run something else, CE Decky says so the first time you start it and offers what it actually found."
              />
            </PanelSectionRow>
          )}
          {!noWindowsProgram && selector !== CUSTOM_PROCESS && !declared.has(selector.toLowerCase()) && fromGameFiles.has(selector.toLowerCase()) && (
            <PanelSectionRow>
              <PanelRow
                status
                testId="review-installed-choice"
                label="Read from this game's files"
                description="The game's own installed executable, not an observation."
                help="Nothing has been seen running yet. If the game turns out to run something else, CE Decky says so the first time you start it and offers what it actually found."
              />
            </PanelSectionRow>
          )}
          {!noWindowsProgram && selector === CUSTOM_PROCESS && (
            <PanelSectionRow><TextField label="Process (.exe basename)" value={customProcess} onChange={traceUiEdit("table_review_modal.process_exe_basename", (event: any) => setCustomProcess(String(event.target.value ?? "")))} disabled={busy} /></PanelSectionRow>
          )}
          {!noWindowsProgram && !targetValid && <PanelSectionRow><Field label="Process required" description="Choose or enter one unambiguous filename ending in .exe. Paths are not accepted." /></PanelSectionRow>}
          {/* The observed process set positively identifies a known anti-cheat.
              The documented boundary is refusal rather than bypass automation,
              and this is the last decision point before an exact table is
              authorized for execution against that game. */}
          {antiCheatReason && (
            <PanelSectionRow><Field label="Anti-cheat detected" description={antiCheatReason} /></PanelSectionRow>
          )}
          {error && <PanelSectionRow><Field label="Could not use table" description={error} /></PanelSectionRow>}
          {/* The step is the only part of this that changes, so it is the only
              part that moves. It used to share one string with the counter and
              with the sentence explaining the wait, and that string was rebuilt
              and re-wrapped every time the step advanced: the row grew and
              shrank under the reader while the words in it changed, which is
              why none of them could be read. The step has the label to itself,
              the explanation is said once and stays, and the counter sits in
              its own column where a digit changing cannot reflow anything. */}
          {busy && step && (
            <PanelSectionRow>
              <PanelRow
                status
                label={step}
                description="Cheat Engine loads the table and answers when it is ready, usually within fifteen seconds on a handheld."
                trailing={(
                  <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
                    {elapsedText(elapsedSeconds)}
                    <Spinner style={{ width: 14, height: 14 }} />
                  </div>
                )}
              />
            </PanelSectionRow>
          )}
          </PanelSection>
        </DensePanel>
        <ModalActions>
          {/* The ring opens on the decision this window is for, and on the way
              out when that decision cannot be made: an anti-cheat refusal is
              final and no press here changes it, and a process that is not yet
              a valid .exe basename is fixed on the control above rather than
              down here. Either way the window must not open on a dead one. */}
          <DialogButton style={modalActionStyle} preferredFocus={usable} disabled={!usable} onClick={traceUiAction("table_review_modal.use_this_table", () => void use())}>Use this table</DialogButton>
          {/* One button for the whole activation, pressable exactly while there
              is a Cheat Engine to stop. It used to be pressable throughout, and
              during the durable writes that run before any launch the only
              thing it could do was report that there was nothing to stop. */}
          {busy && onAbort
            ? <DialogButton style={modalActionStyle} disabled={aborting || !stoppable} onClick={traceUiAction("table_review_modal.abort", () => void abort())}>{aborting ? "Stopping…" : "Stop and cancel"}</DialogButton>
            : <DialogButton style={modalActionStyle} preferredFocus={!usable} disabled={busy || aborting} onClick={traceUiAction("table_review_modal.cancel", () => { if (!busyRef.current && !abortingRef.current) onCancel(); })}>Cancel</DialogButton>}
        </ModalActions>
      </Focusable>
    </ModalRoot>
  );
}
