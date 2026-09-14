import { useUiSurface } from "../useUiSurface";
import { traceUiAction } from "../uiActions";
import { Focusable, ModalRoot } from "@decky/ui";
import { useRef, useState } from "react";
import { ProviderCatalog } from "../providerCatalog";
import { DensePanel, SmallButton } from "../components/PanelDensity";
import type { BlockedLookups, BlockedMark } from "../uiModel";
import type { CompatibilityEvidence, ArtifactResolution, TableStatus } from "../types";
import { describeError } from "../errors";
import { logUiFailure } from "../supportLog";

interface Props {
  compatibility?: readonly CompatibilityEvidence[];
  artifactResolutions?: readonly ArtifactResolution[];
  onRefreshProvenance?: () => Promise<{ resolutions: readonly ArtifactResolution[]; compatibility: readonly CompatibilityEvidence[] }>;
  gameIdentity: string;
  gameName: string;
  /** The game a download belongs to, for the record a failed one may write. */
  appId?: number | null;
  shortcutExecutable?: string | null;
  localArtifacts: Record<string, string>;
  /** Available exact-SHA tables already associated with this game. */
  localTables: readonly TableStatus[];
  /** Device-wide, for exact-digest lookup only; never offered as a row. */
  deviceTables?: readonly TableStatus[];
  /** The provider rows already imported from, for sources with no digest. */
  importedArtifacts: ReadonlySet<string>;
  /** Exact table SHA-256s recorded as not working, to the mark on each. */
  blockedTables: Record<string, BlockedMark>;
  /** The same record keyed by `provider:artifact_id`, for rows with no digest. */
  blockedArtifacts: Record<string, BlockedMark[]>;
  /** Forget the marks on these exact tables so they can be tried again. */
  onClearMarks: (sha256s: string[]) => Promise<void>;
  /** Re-read the marks: this tree never sees its props change once open. */
  onRefreshBlocked: () => Promise<BlockedLookups>;
  onSelected: (sha256: string) => Promise<void>;
  onCancel: () => void;
}

export function TableSearchModal({
  gameIdentity,
  gameName,
  artifactResolutions,
  compatibility,
  onRefreshProvenance,
  appId,
  shortcutExecutable,
  localArtifacts,
  localTables,
  deviceTables,
  importedArtifacts,
  blockedTables,
  blockedArtifacts,
  onClearMarks,
  onRefreshBlocked,
  onSelected,
  onCancel,
}: Props) {
  useUiSurface("TableSearchModal", appId);
  const committingRef = useRef(false);
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const commitSelected = async (sha256: string) => {
    if (committingRef.current) return;
    committingRef.current = true;
    setCommitting(true);
    setError(null);
    try {
      await onSelected(sha256);
    } catch (cause) {
      logUiFailure("search.selection_failed", cause, { app_id: appId, table_sha: sha256 });
      setError(describeError(cause));
    } finally {
      committingRef.current = false;
      setCommitting(false);
    }
  };

  const cancel = () => {
    if (!committingRef.current) onCancel();
  };

  return (
    <ModalRoot onCancel={traceUiAction("table_search_modal.cancel", cancel)}>
      <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
        {/* The search controls are one dense status row rather than a stack
            of full-width buttons, and the row's ellipsis and metrics are scoped
            to this wrapper exactly as they are on Advanced. */}
        <DensePanel>
        {error && <div role="alert">{error}</div>}
        <ProviderCatalog
          gameIdentity={gameIdentity}
          gameName={gameName}
          appId={appId}
          shortcutExecutable={shortcutExecutable}
          initialQuery={gameName}
          autoSearch
          localArtifacts={localArtifacts}
          localTables={localTables}
          deviceTables={deviceTables}
          compatibility={compatibility}
          artifactResolutions={artifactResolutions}
          onRefreshProvenance={onRefreshProvenance}
          importedArtifacts={importedArtifacts}
          blockedTables={blockedTables}
          blockedArtifacts={blockedArtifacts}
          onClearMarks={onClearMarks}
          onRefreshBlocked={onRefreshBlocked}
          onLocalSelected={commitSelected}
          onImported={commitSelected}
          footerActions={<SmallButton disabled={committing} onClick={traceUiAction("table_search_modal.close", cancel)}>Close</SmallButton>}
        />
        </DensePanel>
      </Focusable>
    </ModalRoot>
  );
}
