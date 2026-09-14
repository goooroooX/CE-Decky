import { useUiSurface } from "../useUiSurface";
import { traceUiAction, startUiOperation } from "../uiActions";
import { DropdownItem, Field, Focusable, ModalRoot, PanelSection, PanelSectionRow } from "@decky/ui";
import { useRef, useState, type CSSProperties } from "react";
import type { GameSummary } from "../steam/client";
import { describeError } from "../errors";
import { ActionGroup, BELOW_FIELD_CLASS, DensePanel, SectionHeading, SmallButton } from "../components/PanelDensity";

interface Props {
  games: GameSummary[];
  selectedGame: GameSummary | null;
  /**
   * The games observed running right now, when that is why this is open.
   *
   * More than one real game running is the one case Home cannot resolve by
   * itself, and it is what it asks the user to settle. Handing over the whole
   * installed library without saying which entries caused the question left the
   * two candidates indistinguishable from a few hundred games that had nothing
   * to do with it. Empty for an ordinary "Change game", where the library is
   * exactly what the user came for.
   */
  runningGames?: readonly GameSummary[];
  onPick: (game: GameSummary) => Promise<void> | void;
  onCancel: () => void;
}

function gameKey(game: GameSummary): string {
  return `${game.appId}:${game.isShortcut ? "shortcut" : "steam"}`;
}

export function GamePickerModal({ games, selectedGame, runningGames = [], onPick, onCancel }: Props) {
  useUiSurface("GamePickerModal");
  const running = new Set(runningGames.map(gameKey));
  // Running first, and among them the order they were observed in. Everything
  // else keeps the library's own order underneath.
  const ordered = running.size > 0
    ? [...games].sort((left, right) => Number(running.has(gameKey(right))) - Number(running.has(gameKey(left))))
    : games;
  // Falling back to the first library entry is right when the user opened this
  // to change games: it is the list they came to read. It is wrong when the
  // question is which of several running games they meant, because there the
  // arbitrary answer is one press away from being taken as the deliberate one.
  const initial = selectedGame && ordered.some((game) => gameKey(game) === gameKey(selectedGame))
    ? gameKey(selectedGame)
    : running.size > 0 ? "" : ordered[0] ? gameKey(ordered[0]) : "";
  const [selection, setSelection] = useState(initial);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const game = games.find((candidate) => gameKey(candidate) === selection);
    if (!game || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const operation = startUiOperation("game.select", { app_id: game.appId, shortcut: game.isShortcut });
    try {
      await onPick(game);
      operation.completed();
    } catch (cause) {
      operation.failed(cause);
      setError(describeError(cause));
      busyRef.current = false;
      setBusy(false);
    }
  };

  return (
    <ModalRoot onCancel={traceUiAction("game_picker_modal.cancel_back", () => { if (!busyRef.current) onCancel(); })}>
      <Focusable style={{ minWidth: 420, maxWidth: 600 }}>
        <DensePanel>
          <PanelSection>
            <SectionHeading>Choose game</SectionHeading>
          <PanelSectionRow><Field
            label={running.size > 1 ? "More than one game is running" : "Steam library"}
            description={running.size > 1
              ? "CE Decky cannot tell which of these you are playing. The running games are listed first; the rest of your library is below them."
              : "Select the exact Steam or non-Steam game for this CE Decky profile."}
          /></PanelSectionRow>
          {ordered.length > 0 ? (
            /* The control is stacked under its label here, so the row needs the
               air the dense field padding does not give a stacked one: without
               it the dropdown sits on the bottom edge of its own block. */
            <div className={BELOW_FIELD_CLASS}><PanelSectionRow><DropdownItem
              label="Game"
              layout="below"
              childrenContainerWidth="max"
              contextMenuPositionOptions={{
                bMatchWidth: true,
                bShiftToFitWindow: true,
                bFitToWindow: true,
              }}
              rgOptions={[
                ...(running.size > 0 && !selectedGame ? [{ data: "", label: "Choose a game\u2026" }] : []),
                ...ordered.map((game) => ({
                  data: gameKey(game),
                  label: `${game.name} \u00b7 ${game.isShortcut ? "non-Steam" : `AppID ${game.appId}`}${running.has(gameKey(game)) ? " \u00b7 running" : ""}`,
                })),
              ]}
              selectedOption={selection}
              onChange={traceUiAction("game_picker_modal.game", (option) => setSelection(String(option.data)), (option) => ({ selection: String(option.data) }))}
              disabled={busy}
            /></PanelSectionRow></div>
          ) : (
            <PanelSectionRow><Field label="No games found" description="Refresh the Steam library from Advanced diagnostics." /></PanelSectionRow>
          )}
          {error && <PanelSectionRow><Field label="Selection failed" description={error} /></PanelSectionRow>}
          </PanelSection>
        </DensePanel>
        <div data-testid="game-picker-actions">
          <ActionGroup style={gamePickerActionsStyle}>
            <SmallButton disabled={busy || !selection} onClick={traceUiAction("game_picker_modal.use_this_game", () => void submit(), { selection })}>Use this game</SmallButton>
            <SmallButton disabled={busy} onClick={traceUiAction("game_picker_modal.cancel", () => { if (!busyRef.current) onCancel(); })}>Cancel</SmallButton>
          </ActionGroup>
        </div>
      </Focusable>
    </ModalRoot>
  );
}

const gamePickerActionsStyle: CSSProperties = {
  flexDirection: "row",
  justifyContent: "flex-end",
  width: "100%",
  gap: 8,
  padding: "4px 16px 8px",
  boxSizing: "border-box",
};
