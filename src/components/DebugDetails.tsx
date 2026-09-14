import { traceUiAction } from "../uiActions";
import { PanelSection } from "@decky/ui";
import { useState } from "react";
import { ActionRow, PanelRow, SectionHeading, SmallButton } from "./PanelDensity";
import { clampPage, pageCount, pageItems, stepPage } from "../uiModel";
import type { DiagnosticsSnapshot, FearlessIndexStatus, ProviderDiagnosticsEntry, SessionInventoryApp } from "../types";

/**
 * How many per-app session rows this screen shows at once.
 *
 * This list is the only one on the screen that grows on its own: every game
 * this device has ever prepared a session for keeps a row here, for as long as
 * the session records are kept, so on a device that is actually used it is
 * unbounded in a way nothing else on Debug is. The rest of the screen is a
 * fixed number of rows and belongs above it.
 */
const SESSION_PAGE_SIZE = 6;

interface Props {
  snapshot: DiagnosticsSnapshot | null;
  loading: boolean;
  error: string | null;
  /**
   * What each AppID is called, for the games this device can still name.
   *
   * Every row here used to say `AppID 1971870` and nothing else, which is the
   * one thing on this screen a reader cannot look up without leaving it. The
   * names come from the caller because that is where they are: the Steam
   * library the panel already enumerated, and the not-working records, which
   * carry a game name for a game that is no longer installed. A row whose
   * AppID neither answers keeps the number, because a number is what is known.
   */
  gameNames?: ReadonlyMap<number, string>;
  onRefresh: () => void;
  onBack: () => void;
}

/**
 * The backend's own diagnostics snapshot, rendered densely.
 *
 * `diagnostics_snapshot` already reports everything worth reading when
 * something misbehaves - storage counts, per-provider status, session
 * inventory, every persisted-state error and the capability flags - but nothing
 * ever displayed it, so a controller-only user had no way to see it. It is
 * evidence, not a control surface: no row here mutates anything.
 */
export function DebugDetails({ snapshot, loading, error, gameNames, onRefresh, onBack }: Props) {
  const [sessionPage, setSessionPage] = useState(0);
  const stateErrors: Array<[string, string]> = snapshot
    ? ([
      ["Config", snapshot.config_state_error],
      ["Tables", snapshot.table_state_error],
      ["Profiles", snapshot.profile_state_error],
      ["Providers", snapshot.provider_state_error],
      ["Sessions", snapshot.session_state_error],
    ] as Array<[string, string | null]>).flatMap(([name, message]) => message ? [[name, message] as [string, string]] : [])
    : [];
  const capabilities = snapshot ? Object.entries(snapshot.capabilities) : [];

  return (
    <>
      <PanelSection>
        <SectionHeading>Debug details</SectionHeading>
        <ActionRow testId="debug-actions">
          <SmallButton disabled={loading} onClick={traceUiAction("debug_details.refresh", onRefresh)}>Refresh</SmallButton>
          <SmallButton onClick={traceUiAction("debug_details.back", onBack)}>Back</SmallButton>
        </ActionRow>
        {error && <PanelRow testId="debug-error" label="Could not read diagnostics" description={error} />}
        {loading && !snapshot && <PanelRow label="Reading…" description="Collecting the backend diagnostics snapshot." />}
        {snapshot && (
          <>
            <PanelRow truncate label={`CE Decky ${snapshot.version}`} description={`up ${Math.round(snapshot.uptime_s)}s · ${snapshot.log_path}`} />
            <PanelRow
              truncate
              label="Storage"
              description={`${snapshot.storage.tables} tables · ${Math.round(snapshot.storage.table_bytes / 1024)} KiB · ${snapshot.storage.profiles} profiles`}
            />
            <PanelRow
              truncate
              label="Sessions"
              description={`${snapshot.sessions.total_sessions} total across ${snapshot.sessions.apps.length} app(s)${snapshot.sessions.errors.length ? ` · ${snapshot.sessions.errors.length} unreadable` : ""}`}
            />
            {snapshot.fearless_index && (
              <PanelRow
                truncate
                testId="debug-fearless-index"
                label="FearLess index"
                description={describeFearlessIndex(snapshot.fearless_index)}
                help="CE Decky reads the FearLess table forum's own listing pages instead of its search route, and keeps them as a local index. Every table search re-reads the newest pages; a page is read again once a day, so this shows how much of the index is currently within that day and what the last background pass actually fetched."
              />
            )}
          </>
        )}
      </PanelSection>

      {snapshot && (Object.keys(snapshot.providers).length > 0 || snapshot.provider_selection) && (
        <PanelSection>
          <SectionHeading>Providers</SectionHeading>
          {/* Stated before the counters, because it is what makes them
              readable: a source that was never asked and a source that was
              asked and found nothing both record zero results, and only this
              row says which happened. */}
          {snapshot.provider_selection && (
            <PanelRow
              truncate
              testId="debug-provider-selection"
              label="Switched off"
              description={snapshot.provider_selection.reason
                ? `${snapshot.provider_selection.reason} (every source is being searched)`
                : snapshot.provider_selection.disabled.join(", ") || "None"}
            />
          )}
          {Object.entries(snapshot.providers).map(([id, entry]) => (
            <PanelRow
              key={id}
              truncate
              testId={`debug-provider-${id}`}
              label={`${id}${entry.retired ? " (retired)" : ""}${switchedOff(snapshot, id) ? " (off)" : ""}`}
              description={describeProvider(entry)}
            />
          ))}
        </PanelSection>
      )}

      {snapshot && (stateErrors.length > 0 || snapshot.table_catalog_errors.length > 0) && (
        <PanelSection>
          <SectionHeading>State errors</SectionHeading>
          {stateErrors.map(([name, message]) => (
            <PanelRow key={name} truncate testId={`debug-state-${name}`} label={name} description={message} />
          ))}
          {snapshot.table_catalog_errors.map((entry) => (
            <PanelRow key={entry.path} truncate label={entry.path} description={entry.error} />
          ))}
        </PanelSection>
      )}

      {capabilities.length > 0 && (
        <PanelSection>
          <SectionHeading>Capabilities</SectionHeading>
          {/* One line each like every other row on this screen, opened by a
              press when the list is longer than the row. */}
          <PanelRow
            truncate
            testId="debug-capabilities-enabled"
            label="Enabled"
            description={capabilities.filter(([, on]) => on).map(([name]) => name).join(", ") || "None"}
          />
          <PanelRow
            truncate
            testId="debug-capabilities-disabled"
            label="Disabled"
            description={capabilities.filter(([, on]) => !on).map(([name]) => name).join(", ") || "None"}
          />
        </PanelSection>
      )}

      {/* Last on the screen, because it is the only list here that grows on its
          own: one row per game this device has ever prepared a session for. It
          used to sit between the storage totals and the providers, so a screen
          read for a provider counter had a list of every game in the way of it,
          and the rows below it were reached by paging past however many games
          this device happens to hold. */}
      {snapshot && snapshot.sessions.apps.length > 0 && (() => {
        const apps = orderedSessionApps(snapshot.sessions.apps);
        const pages = pageCount(apps.length, SESSION_PAGE_SIZE);
        const safePage = clampPage(sessionPage, apps.length, SESSION_PAGE_SIZE);
        const shown = pageItems(apps, safePage, SESSION_PAGE_SIZE);
        const troubled = apps.filter(hasSessionProblem).length;
        return (
          <PanelSection>
            <SectionHeading>Sessions by app</SectionHeading>
            {/* The row that heads the list says what paging through it would
                otherwise be the only way to find out. */}
            <PanelRow
              tone="header"
              truncate
              testId="debug-sessions-summary"
              label={`${apps.length} app(s) · ${snapshot.sessions.total_sessions} session(s)`}
              description={troubled
                ? `${troubled} with something to read · worst first`
                : "Nothing unreadable · most sessions first"}
            />
            {shown.map((app) => (
              <PanelRow
                key={app.app_id}
                truncate
                scroll
                testId={`debug-session-${app.app_id}`}
                label={sessionAppLabel(app, gameNames)}
                description={[
                  `${app.session_count} session(s)`,
                  app.current_session_id ? `current ${app.current_session_id.slice(0, 8)}` : "no current session",
                  app.corrupt_entries ? `${app.corrupt_entries} corrupt` : null,
                  app.current_error,
                ].filter(Boolean).join(" · ")}
              />
            ))}
            {pages > 1 && (
              <PanelRow
                truncate
                testId="debug-sessions-pager"
                label={`Page ${safePage + 1} of ${pages}`}
                description={`Showing ${shown.length} of ${apps.length}`}
                actions={(
                  <>
                    <SmallButton
                      disabled={safePage === 0}
                      onClick={traceUiAction("debug_details.sessions_previous", () => setSessionPage(stepPage(safePage, apps.length, SESSION_PAGE_SIZE, -1)), { page: safePage - 1 })}
                    >Previous</SmallButton>
                    <SmallButton
                      disabled={safePage >= pages - 1}
                      onClick={traceUiAction("debug_details.sessions_next", () => setSessionPage(stepPage(safePage, apps.length, SESSION_PAGE_SIZE, 1)), { page: safePage + 1 })}
                    >Next</SmallButton>
                  </>
                )}
              />
            )}
          </PanelSection>
        );
      })()}
    </>
  );
}

/** Whether this app's session records have anything a reader has to act on. */
function hasSessionProblem(app: SessionInventoryApp): boolean {
  return Boolean(app.corrupt_entries) || Boolean(app.current_error);
}

/**
 * The order a paged list of games is read in, fixed so a page cannot reshuffle.
 *
 * Anything unreadable first, because that is the only reason to open this
 * screen, and a reader should not have to page through every game that is
 * working to find the one that is not. Then by how much this device holds for
 * the game, then by AppID, which is unique and settles every tie.
 */
function orderedSessionApps(apps: readonly SessionInventoryApp[]): SessionInventoryApp[] {
  return [...apps].sort((left, right) =>
    Number(hasSessionProblem(right)) - Number(hasSessionProblem(left))
    || right.session_count - left.session_count
    || left.app_id - right.app_id);
}

/**
 * What one row is called: the game where this device can still name it.
 *
 * The AppID stays on the row either way. It is what every other record here is
 * keyed by, it is what a bug report quotes, and a game name is not a
 * substitute for it.
 */
function sessionAppLabel(app: SessionInventoryApp, gameNames?: ReadonlyMap<number, string>): string {
  const name = gameNames?.get(app.app_id);
  return name ? `${name} · AppID ${app.app_id}` : `AppID ${app.app_id}`;
}

/** Whether this provider is one the user switched off. */
function switchedOff(snapshot: DiagnosticsSnapshot, providerId: string): boolean {
  return Boolean(snapshot.provider_selection?.disabled.includes(providerId));
}

/** How long ago, in the coarsest unit that still reads as a number. */
function ago(epochSeconds: number | null): string | null {
  if (!epochSeconds) return null;
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 90 * 60) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 48 * 3600) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function describeFearlessIndex(index: FearlessIndexStatus): string {
  const coverage = index.total_pages
    ? `${index.indexed_pages}/${index.total_pages} pages`
    : `${index.indexed_pages} page(s)`;
  const refreshed = index.fully_refreshed_at
    ? `fully refreshed ${ago(index.fully_refreshed_at)}`
    // Without a complete index there is no "whole index is this fresh" answer;
    // the number of pages still owed is the honest one.
    : `${index.stale_pages} page(s) to read`;
  const lastPass = index.last_refresh_at
    ? `last pass ${index.last_refresh_pages} page(s) ${ago(index.last_refresh_at)}`
    : "no background pass yet";
  return [
    index.status,
    coverage,
    `${index.indexed_topics} topics`,
    refreshed,
    index.fully_refreshed_at && index.stale_pages ? `${index.stale_pages} due` : null,
    lastPass,
    index.retry_after_seconds ? `paused ${index.retry_after_seconds}s` : null,
    index.error,
  ].filter(Boolean).join(" · ");
}

function describeProvider(entry: ProviderDiagnosticsEntry): string {
  const counters = entry.counters;
  return [
    entry.retired ? "historical provider" : null,
    entry.state,
    `${counters.searches} searches`,
    `${counters.results} results`,
    `${counters.downloads_succeeded}/${counters.downloads_succeeded + counters.downloads_failed} downloads`,
    counters.errors ? `${counters.errors} errors` : null,
    // A wait is not a failure, so it is named apart from one: a provider that
    // throttles every transfer otherwise reads as one that simply serves files,
    // and the user who watched the countdown has nothing to point at.
    counters.downloads_throttled
      ? `${counters.downloads_throttled} throttled${entry.last_throttle_wait_s ? ` (last ${entry.last_throttle_wait_s}s)` : ""}`
      : null,
    // A source that quietly stops being readable otherwise looks like a game
    // with no tables. An unreadable page is the critical one: nothing on it can
    // be downloaded. Dropped description is not, so it is named apart.
    counters.parse_failed ? `${counters.parse_failed} unreadable page(s)` : null,
    counters.parse_degraded ? `${counters.parse_degraded} partly read` : null,
    entry.last_http_status !== null ? `HTTP ${entry.last_http_status}` : null,
    entry.last_latency_ms !== null ? `${entry.last_latency_ms}ms` : null,
    entry.last_error,
  ].filter(Boolean).join(" · ");
}
