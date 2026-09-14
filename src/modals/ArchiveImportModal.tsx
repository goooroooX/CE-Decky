import { useUiSurface } from "../useUiSurface";
import { traceUiAction, traceUiEdit, startUiOperation } from "../uiActions";
import { DialogButton, DropdownItem, Field, Focusable, ModalRoot, PanelSection, PanelSectionRow, TextField } from "@decky/ui";
import { useRef, useState } from "react";
import type { ArchiveMember } from "../types";
import { ModalActions, modalActionStyle } from "../components/ModalActions";
import { describeError } from "../errors";
import { UNSUPPORTED_MEMBER_EXPLANATION, isUnsupportedEncryptedMember } from "../tableImport";
import { DensePanel, SectionHeading } from "../components/PanelDensity";

interface Props {
  members: ArchiveMember[];
  onImport: (memberPath: string, password: string | null) => Promise<void> | void;
  onCancel: () => void;
}

export function ArchiveImportModal({ members, onImport, onCancel }: Props) {
  useUiSurface("ArchiveImportModal");
  const [memberPath, setMemberPath] = useState(members[0]?.path ?? "");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const member = members.find((candidate) => candidate.path === memberPath) ?? null;
  const unsupported = isUnsupportedEncryptedMember(member);
  // Whether the press this window exists for can be made at all right now.
  const importable = !busy && Boolean(member) && !unsupported && !(member?.encrypted && !password);

  const submit = async () => {
    if (!member || busyRef.current || unsupported || (member.encrypted && !password)) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const operation = startUiOperation("archive.import", { member_index: members.indexOf(member), encrypted: member.encrypted });
    try {
      await onImport(member.path, member.encrypted ? password : null);
      operation.completed();
    } catch (cause) {
      operation.failed(cause);
      setError(describeError(cause));
      busyRef.current = false;
      setBusy(false);
    }
  };

  return (
    <ModalRoot onCancel={traceUiAction("archive_import_modal.cancel_back", () => { if (!busyRef.current) onCancel(); })}>
      <Focusable style={{ minWidth: 420, maxWidth: 600 }}>
        <DensePanel>
          <PanelSection>
            <SectionHeading>Choose table from archive</SectionHeading>
          <PanelSectionRow><DropdownItem
            label="Table"
            rgOptions={members.map((item) => ({ data: item.path, label: `${item.path} · ${Math.max(1, Math.ceil(item.size / 1024))} KiB${item.encrypted ? " · encrypted" : ""}` }))}
            selectedOption={memberPath}
            onChange={traceUiAction("archive_import_modal.table", (option) => { setMemberPath(String(option.data)); setPassword(""); }, (option) => ({ member_index: members.findIndex((item) => item.path === String(option.data)) }))}
            disabled={busy}
          /></PanelSectionRow>
          {/* Asking for a password CE Decky will not transport is worse than
              saying so: no entry can ever satisfy an encrypted 7z. */}
          {unsupported && (
            <PanelSectionRow>
              <Field
                label="This entry cannot be imported"
                description={UNSUPPORTED_MEMBER_EXPLANATION}
              />
            </PanelSectionRow>
          )}
          {member?.encrypted && !unsupported && <PanelSectionRow><TextField label="Archive password (not stored)" value={password} onChange={traceUiEdit("archive_import_modal.archive_password_not_stored", (event: any) => setPassword(String(event.target.value ?? "")))} disabled={busy} /></PanelSectionRow>}
          {error && <PanelSectionRow><Field label="Import failed" description={error} /></PanelSectionRow>}
          </PanelSection>
        </DensePanel>
        <ModalActions>
          {/* The ring opens on the press this window exists for, and on the way
              out when that press cannot be made: an entry this build cannot
              open, or one still waiting for a password, leaves Import dead, and
              a window that opens on a dead control answers nothing. */}
          <DialogButton style={modalActionStyle} preferredFocus={importable} disabled={!importable} onClick={traceUiAction("archive_import_modal.import", () => void submit())}>Import</DialogButton>
          <DialogButton style={modalActionStyle} preferredFocus={!importable} disabled={busy} onClick={traceUiAction("archive_import_modal.cancel", () => { if (!busyRef.current) onCancel(); })}>Cancel</DialogButton>
        </ModalActions>
      </Focusable>
    </ModalRoot>
  );
}
