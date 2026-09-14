# UI action audit, 0.9.22

Source baseline: `8bd20852184837c1ac9954d8dcb77349bb5781c3`. This audit covers owned handlers on Home, every modal and nested screen, repeated rows, pagers, help expanders and native confirmation callbacks. The inventory below is checked against JSX by `tests/uiActionCoverage.test.ts`, including the aliased Decky confirmation component. `SmallButton` and `CheatRow` forward callbacks traced at their call sites.

## Routes and critical boundaries

| Surface / decision | Functional route and result owner |
|---|---|
| Home setup, retry, reinstall confirmation, cancel | `index.tsx` managed setup functions; capability read, operation start/poll/complete/cancel through `api.ts`. `ManagedSetupOwner` keeps one observer through cancellation and lost receipts; the old CE remains usable until replacement commits. |
| Choose/change game, picker selection and Cancel | `GamePickerModal` to `hydrateGame`, Steam identity reads and profile hydration. The picker records the selected AppID/shortcut identity. Immediate Cancel cannot escape a pending selection. |
| Home Search, query, retry, clear marks, result rows, saved-copy decision and paging | `TableSearchModal` and `ProviderCatalog`; local SHA selection or provider/search/artifact acquisition authority. Catalog owns search failures and context generations; Search owns selection/review handoff failures inline. Saved copies keep the offline route; download is not consent. |
| Manage, filter, paging, Use, Local file, Revoke/Confirm, Delete/Confirm and Back | `ImportedTablesModal`; exact SHA to the parent's select/delete callback. Selection, revocation and deletion share a synchronous latch; holder counts gate Delete, with the backend rechecking. Manage also works without a selected game; confirmed revocation stops owned CE and detaches the captured exact table from its holders. Failed callbacks leave the row and release the latch. |
| Native file picker, archive member/password, Import and Cancel | `openLocalTable` / `pickCE`; native picker cancellation is distinguished from selection. Archive import latches until handoff; entered passwords never enter action fields. Native picker internals belong to Decky; only its invocation and returned decision are observable here. |
| Acquisition status retry, member/password, import and cancellation | `TableAcquisitionModal`; backend acquisition ID is authoritative, polling is bounded and a lost completion receipt is reconciled before another import. Cancellation failures and handoff failures remain distinct. |
| Review, rescan, process choice, code view, consent/use and Stop and cancel | `TableReviewModal` to `activateTable`; exact SHA review and process validation precede consent/session preparation. Stopping and using have separate immediate latches. Look inside only reads code. |
| Code section list, Read, Show more, Previous/Next and both Back routes | `TableCodeModal`; bounded section index/body RPCs and bounded page slices. One read starts per immediate press burst; read failures release the latch. No code executes in this view. |
| Configure cheats: scripts, section, filter, More/Less, dropdown/value, pin, active and pages | `CheatSelectionModal`; values/active flags are staged, pins persist separately. Logs identify record IDs without entered values. Applying prevents concurrent edits/pinning/closing. |
| Configure Apply, close confirmation Apply/Discard/Keep editing | Startup-plan validation, configured-value persistence, exact-session runtime commands, readback and remembered-state persistence. Failure reports partial durable/runtime changes and reconciles them. Discard drops staged edits only and cannot race an Apply already started. |
| Home pinned toggle, Disable all, Auto-load, start/stop CE | `index.tsx` and `runtimeClient.ts`; exact game/table/session, runtime readback and desired-state persistence. `runAction` logs its captured interaction through completion/failure. Auto-load still revalidates after awaits and remains an automatic operation. |
| Advanced Refresh, Self-test, Debug and debug Refresh/Back | `AdvancedModal.invoke`, `refreshAdvancedContext` and diagnostic RPCs. Direct diagnostics errors now have an operation failure record even when there is no parent action wrapper. |
| Advanced CE Import/Test/Forget, Proton choice/Verify | Exact CE identity and launch self-test callbacks in `index.tsx`; selection alone never establishes compatibility. Native import picker decisions are logged without filesystem paths. |
| Advanced target Save, Processes, observed target Save, exact PID Retry attach, Stop CE and save | Process/launch capabilities to exact target callbacks. Logs carry selected process/PID; backend target validation and launch ownership remain authoritative. |
| Advanced profile/session/ownership repairs | Typed repair callback, followed by status reconciliation on success or failure. Post-publication durability uncertainty is not reported as a clean refusal. |
| Advanced table source Open, Look inside, consent Revoke, startup Clear | Source navigation closes the owned modal; reading code remains separate from consent. Profile mutations retain desired-state readback. |
| Advanced Sources: switches, Switch all on, Reset counts and Back | Provider selection/diagnostics callbacks. Source IDs and requested enabled state are logged. Controller Back cannot leave during a source mutation. |
| Advanced blocked tables: Clear, Clear all, Show more and Back | Exact blocked-key mutations and refreshed list. Advisory marks stay reversible and do not delete stored tables. |
| Advanced removal Check, scope, Delete these, Keep it/Delete confirmation, Cancel/Back | Backend readiness and confirmed managed deletion. The detached snapshot is reconciled after success or failure; failed deletion's readiness read no longer attempts to acquire its own occupied UI latch. |
| Support Collect, saved-bundle OK/Back; action-failure Close/Back | The current frontend ring is passed to `createSupportBundle`; collection has its own operation outcome, and the saved dialog explicitly closes. The collection's later outcome necessarily appears only in a subsequent bundle. |
| Shared help and text expansion | `PanelRow` logs the row and requested open state; presentational forwarders do not duplicate the action record. |

All backend routes continue through `api.ts`, `plugin.py`, `OperationRegistry` and the guarded service/runtime helpers. Backend activity logs carry their own operation identities and redaction. Frontend interaction numbers are local to the loaded frontend, not backend correlation IDs; use timestamps plus AppID/SHA/acquisition/record identities to connect the two logs.

## Evidence and limits

The follow-up against `9e0bb7806de66307ee38b090f9485d4128965f79` replaces registration heuristics with the exact completion receipt for consumed operations, including forced reinstall and a successor operation. Cancel preempts unavailable ordinary reconciliation while its own failed receipt still requires reconciliation. A completed predecessor remains completed across handoff. Owner capability reads cannot independently publish late React state. Setup failure has one visible recovery-dialog owner; the outer action logs its failure without opening a second modal. Owner and workflow regressions cover these paths and remain in the ordinary release selection; receipt reconciliation logs use the existing support-bundle ring.

The follow-up against `a212f7608eed32a9c24d998f3a674723e19e7ccc` scopes pending/confirmed/failed Stop to one activation and waits for the exact Stop receipt before success or panel release. Setup mutation promises have no read deadline; simulated long start, cancel and complete calls remain owned without a premature terminal event. Terminal A handing observation to newer B and a fresh start refusal beside unchanged failed/cancelled A have regressions in `tests/managedSetup.test.ts`. The workflow tests exercise pending Stop A, its successful and failed outcomes, and a prepared B that never inherits A's state. These checks stay in the ordinary release selection.

The follow-up against `1cc5f38e92edab1554f340e4a7e5839aed484283` distinguishes a pending Stop from confirmed stopping: rejection or nothing-to-stop releases the latch, and only a confirmed stop marks activation aborted. `ManagedSetupOwner` serializes receipt recovery, polling, cancellation and completion; Home disables Cancel while its result is pending. Reconciliation adopts backend truth and retains the exact active operation's monitor after a lost response. Search owns selection/review preparation errors inline for both local and acquisition handoffs, with the parent's failure modal suppressed. Regressions exercise these failure paths as well as successful completion. `tests/managedSetup.test.ts` additionally checks late poll replies, unavailable reconciliation and observer cleanup with simulated timers; `tests/tableSearchHandoff.test.tsx` checks both inline error entrypoints. Both are included in the ordinary release Vitest selection, alongside the workflow and detached-modal regressions.

The static follow-up against `cef1aeb33c972668bd0998be2ee4f23caccb2aa6` tightens these boundaries: A confirmed Stop stays latched until both the activation and its cancellation settle, and pending pin writes disable conflicting Active/Value and close-prompt mutations. A deferred Stop using it answer captures its interaction on the press and explicitly passes it to `runAction` after waiting. Acquisition and managed-setup cancellation record their exact operation identity before the RPC and one terminal outcome. Resuming an existing setup operation is automatic work without a fabricated interaction. Component regressions cover both stop-completion orders, pending pin writes, deferred refusal answers, cancellation success/failure (including completed native promotion) and automatic setup continuation.

`ui.action` records a callback invocation, including one a busy guard refuses. `ui.edit` records an editing burst without its text. `ui.operation_started/completed/failed` and the panel/domain events report the actual asynchronous outcome. A completed wrapper is not proof that every later detached refresh or launch succeeded; those have separate error/domain records. `ui.surface_opened/closed` reports component lifetime, not a guessed reason for host-driven closure.

The existing 500-entry frontend ring and dropped count are unchanged. Continuous typing in one field is coalesced until a different action/field or a pause. No event arguments, password contents, queries, typed cheat values, code bodies or provider URLs are serialized by action tracing. The backend collector regression checks that action/operation identity survives `logs/frontend.json`. A frontend reload loses this in-memory history; backend logs and current state remain available. This is not a persistent full-session replay.

Focused regressions cover synchronous Manage failures, same-turn Cancel, repeated Stop and code Read, Apply/Discard races, source navigation during writes, failed-removal reconciliation, trace privacy, correlation and inventory coverage. Existing workflow, catalog, runtime, support-log and support-bundle tests cover the remaining routes. Run through `python scripts/qa.py`; the release profile is the package gate.

### Automatic release checks and cost

The ordinary `release` profile already discovers all Vitest and backend tests. This change adds no separate release stage, provider sweep, live CE launch or browser/device traversal:

- `tests/uiActionCoverage.test.ts` parses local TSX, resolves Decky aliases, checks direct handlers and forwarding boundaries, requires coalesced text-field edits and compares the action IDs with this inventory.
- `tests/uiActions.test.ts` checks argument privacy, overlapping-operation correlation, original return/error behavior and bounded typing logs.
- The existing detached-modal, code-modal and workflow suites gain focused cases for the races and failure paths listed above. Modal lifetime evidence also covers host-driven unmounts. A workflow regression opens Advanced from the real panel component, fails Self-test, presses Collect and checks that those action/failure events reach `createSupportBundle` through the production callback, including the Collect press itself.
- `tests/test_support_bundle.py` checks that the action and operation identity reaches the existing frontend log member of a real locally generated support ZIP.

A single local QA sample on Valve Steam Machine Fremont, SteamOS 3.8.16, using `python3 scripts/qa.py --vitest tests/uiActionCoverage.test.ts --vitest tests/tableCodeModal.test.tsx --pytest tests/test_support_bundle.py`, reported 106 ms for the inventory test and 414 ms for the entire code-modal file. These are test execution times, not process startup costs or upper bounds. The broader workflow suite was already part of release; the new cases extend that run rather than duplicating it. These checks are retained in ordinary release because they are local and small. Interactive Game Mode checks remain separate.

Source tracing and mocked component tests do not establish Steam controller navigation, host close-icon behavior, focus/scroll visibility on each viewport, real CE process attachment or power-loss behavior. The installed package must be exercised in Game Mode for those observations. No row under FIELD_NOTES' Still worth checking is closed by this audit.

## Handler inventory

Each identifier below names one owned callback location, even when several rows render it. Identity fields distinguish the selected row. Source paths are relative to the repository root.

| Action identifier | Source | Callback |
|---|---|---|
| `action_failure_modal.close` | `src/modals/ActionFailureModal.tsx` | `onClose` |
| `action_failure_modal.on_close` | `src/modals/ActionFailureModal.tsx` | `onClose` |
| `advanced_modal.back_2` | `src/modals/AdvancedModal.tsx` | `() => { if (!busy && !localBusyRef.current) setSourcesOpen(false); }` |
| `advanced_modal.back_3` | `src/modals/AdvancedModal.tsx` | `() => { if (!busy && !localBusyRef.current) setBlockedOpen(false); }` |
| `advanced_modal.back` | `src/modals/AdvancedModal.tsx` | `() => { if (!busy && !localBusyRef.current) setRemovalOpen(false); }` |
| `advanced_modal.blocked.back` | `src/modals/AdvancedModal.tsx` | `() => { if (!busy && !localBusyRef.current) setBlockedOpen(false); }` |
| `advanced_modal.blocked.clear` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(() => onUnblockTable(blockedKey(entry)), () => { void reloadBlockedTables(); });...` |
| `advanced_modal.blocked.show_more` | `src/modals/AdvancedModal.tsx` | `() => setBlockedPages((pages) => pages + 1)` |
| `advanced_modal.cancel` | `src/modals/AdvancedModal.tsx` | `() => { if (!busy && !localBusyRef.current) setDeleteScope(null); }` |
| `advanced_modal.check` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onCheckRemoval, (next) => { setRemoval(next); openSubScreen("removal", () => set...` |
| `advanced_modal.choose` | `src/modals/AdvancedModal.tsx` | `() => openSubScreen("sources", () => { setSourcesOpen(true); void reloadProviderSources(); })` |
| `advanced_modal.clear_all` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onClearBlockedTables, () => { setBlockedPages(1); void reloadBlockedTables(); })...` |
| `advanced_modal.close_2` | `src/modals/AdvancedModal.tsx` | `close` |
| `advanced_modal.close` | `src/modals/AdvancedModal.tsx` | `close` |
| `advanced_modal.collect` | `src/modals/AdvancedModal.tsx` | `() => { setSupportBundleError(null); void collectSupportBundle(); }` |
| `advanced_modal.consent.revoke` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onRevokeConsent); }` |
| `advanced_modal.debug.back` | `src/modals/AdvancedModal.tsx` | `() => setDebugOpen(false)` |
| `advanced_modal.debug` | `src/modals/AdvancedModal.tsx` | `() => openSubScreen("debug", openDebug)` |
| `advanced_modal.delete.cancel_back` | `src/modals/AdvancedModal.tsx` | `() => { if (!busy && !localBusyRef.current) setDeleteScope(null); }` |
| `advanced_modal.delete.confirm` | `src/modals/AdvancedModal.tsx` | `() => { confirm.Close(); void performDelete(deleteScope); }` |
| `advanced_modal.delete.keep` | `src/modals/AdvancedModal.tsx` | `() => confirm.Close()` |
| `advanced_modal.delete_these` | `src/modals/AdvancedModal.tsx` | `() => { const confirm = showModal( <ConfirmModal strTitle="Delete this plugin data?" strDescription=...` |
| `advanced_modal.delete` | `src/modals/AdvancedModal.tsx` | `() => { setDeleteError(null); setDeleted(null); setDeleteScope("cache"); }` |
| `advanced_modal.exact_process_pid` | `src/modals/AdvancedModal.tsx` | `(option) => setAttachCandidate(String(option.data))` |
| `advanced_modal.forget` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onClearCEImport, reconcileContext); }` |
| `advanced_modal.import` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onPickCE); }` |
| `advanced_modal.look_inside` | `src/modals/AdvancedModal.tsx` | `() => openSubScreen("code", () => setCodeOpen(true))` |
| `advanced_modal.open` | `src/modals/AdvancedModal.tsx` | `() => { openSourcePage(tableOrigin.source_page); }` |
| `advanced_modal.ownership.repair` | `src/modals/AdvancedModal.tsx` | `() => { void repair(onRepairOwnedLaunchState); }` |
| `advanced_modal.processes` | `src/modals/AdvancedModal.tsx` | `() => { if (runtimeSessionReady) { void invoke(onRefreshProcesses, (next) => { setRuntimeView(next);...` |
| `advanced_modal.profile.discard` | `src/modals/AdvancedModal.tsx` | `() => { void repair(onRepairProfileState); }` |
| `advanced_modal.refresh_library` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onRefreshGames, setGamesView); }` |
| `advanced_modal.refresh_runtime` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onRefreshRuntime, setRuntimeView); }` |
| `advanced_modal.refresh` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onRefreshAll, reconcileContext); }` |
| `advanced_modal.removal.back` | `src/modals/AdvancedModal.tsx` | `() => { if (!busy && !localBusyRef.current) setRemovalOpen(false); }` |
| `advanced_modal.reset_counts` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onResetProviderDiagnostics, (next) => { void reloadProviderSources(next); }, (ca...` |
| `advanced_modal.retry_attach` | `src/modals/AdvancedModal.tsx` | `() => { if (!selectedAttach) return; void invoke(() => onRetryAttach(selectedAttach.name, selectedAt...` |
| `advanced_modal.review` | `src/modals/AdvancedModal.tsx` | `() => openSubScreen("blocked", () => { setBlockedPages(1); setBlockedOpen(true); })` |
| `advanced_modal.save_as_target` | `src/modals/AdvancedModal.tsx` | `() => { if (!selectedObservedTarget) return; void invoke(() => onSaveTargetProcess(selectedObservedT...` |
| `advanced_modal.save` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(() => onSaveTargetProcess(targetDraft)); }` |
| `advanced_modal.self_test_proton` | `src/modals/AdvancedModal.tsx` | `(option) => { const value = String(option.data); setProtonDraft(value); onLaunchProtonChange(value);...` |
| `advanced_modal.self_test` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onRunSelfTest, setSelfTestView); }` |
| `advanced_modal.session.discard` | `src/modals/AdvancedModal.tsx` | `() => { void repair(onRepairSessionState); }` |
| `advanced_modal.source.toggle` | `src/modals/AdvancedModal.tsx` | `() => { void invoke( () => onSetProviderEnabled(source.provider, !source.enabled), (next) => { void ...` |
| `advanced_modal.sources.back` | `src/modals/AdvancedModal.tsx` | `() => { if (!busy && !localBusyRef.current) setSourcesOpen(false); }` |
| `advanced_modal.startup.clear` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onClearStartup, setStartupCount); }` |
| `advanced_modal.stop_ce_and_save` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(() => onSaveTargetProcess(liveTargetOverride)); }` |
| `advanced_modal.support_bundle_dismiss` | `src/modals/AdvancedModal.tsx` | `() => confirmation.Close()` |
| `advanced_modal.support_bundle_saved` | `src/modals/AdvancedModal.tsx` | `() => confirmation.Close()` |
| `advanced_modal.switch_all_on` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(onResetProviderSources, (next) => { void reloadProviderSources(next); }, (cause)...` |
| `advanced_modal.target_from_observed_processes` | `src/modals/AdvancedModal.tsx` | `(option) => setObservedTargetDraft(String(option.data))` |
| `advanced_modal.target_process` | `src/modals/AdvancedModal.tsx` | `(event: any) => setTargetDraft(String(event.target.value ?? ""))` |
| `advanced_modal.test` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(() => onRunCELaunchSelfTest(protonDraft)); }` |
| `advanced_modal.use_running_target` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(() => onSaveTargetProcess(absentTargetCandidates[0])); }` |
| `advanced_modal.verify` | `src/modals/AdvancedModal.tsx` | `() => { void invoke(() => onRunCELaunchSelfTest(protonDraft)); }` |
| `advanced_modal.what_to_delete` | `src/modals/AdvancedModal.tsx` | `(option) => { setDeleteError(null); setDeleteScope(String(option.data) as ManagedDataScope); }` |
| `archive_import_modal.archive_password_not_stored` | `src/modals/ArchiveImportModal.tsx` | `(event: any) => setPassword(String(event.target.value ?? ""))` |
| `archive_import_modal.cancel_back` | `src/modals/ArchiveImportModal.tsx` | `() => { if (!busyRef.current) onCancel(); }` |
| `archive_import_modal.cancel` | `src/modals/ArchiveImportModal.tsx` | `() => { if (!busyRef.current) onCancel(); }` |
| `archive_import_modal.import` | `src/modals/ArchiveImportModal.tsx` | `() => void submit()` |
| `archive_import_modal.table` | `src/modals/ArchiveImportModal.tsx` | `(option) => { setMemberPath(String(option.data)); setPassword(""); }` |
| `catalog.clear_marks` | `src/providerCatalog.tsx` | `() => void clearMarks()` |
| `catalog.local_table` | `src/providerCatalog.tsx` | `() => showExistingChoice(table, { provenance: "local_only" })` |
| `catalog.result` | `src/providerCatalog.tsx` | `() => { if (localTable && onLocalSelected) showExistingChoice(localTable, { result, provenance }); e...` |
| `catalog.saved_choice.cancel` | `src/providerCatalog.tsx` | `onClose` |
| `catalog.search` | `src/providerCatalog.tsx` | `() => void search()` |
| `cheat_selection_modal.active` | `src/modals/CheatSelectionModal.tsx` | `(checked) => touchActive(recordId, checked)` |
| `cheat_selection_modal.apply_2` | `src/modals/CheatSelectionModal.tsx` | `() => void apply()` |
| `cheat_selection_modal.apply` | `src/modals/CheatSelectionModal.tsx` | Guard applying/pinning, dismiss the prompt and call `apply` |
| `cheat_selection_modal.cancel` | `src/modals/CheatSelectionModal.tsx` | `requestClose` |
| `cheat_selection_modal.discard` | `src/modals/CheatSelectionModal.tsx` | `() => { if (applyingRef.current \|\| pinningRef.current) return; setConfirmingClose(false); setStaged(...` |
| `cheat_selection_modal.edit_value` | `src/modals/CheatSelectionModal.tsx` | `(event: any) => touchValue(recordId, String(event.target.value ?? ""))` |
| `cheat_selection_modal.expand_record` | `src/modals/CheatSelectionModal.tsx` | `() => setExpanded((current) => current === recordId ? null : recordId)` |
| `cheat_selection_modal.filter` | `src/modals/CheatSelectionModal.tsx` | `(event: any) => { setSearch(String(event.target.value ?? "")); setPage(0); setExpanded(null); }` |
| `cheat_selection_modal.find_a_value` | `src/modals/CheatSelectionModal.tsx` | `(event: any) => setValueQuery({ id: recordId, text: String(event.target.value ?? "") })` |
| `cheat_selection_modal.keep_editing` | `src/modals/CheatSelectionModal.tsx` | `() => setConfirmingClose(false)` |
| `cheat_selection_modal.pinned` | `src/modals/CheatSelectionModal.tsx` | `(checked) => void togglePin(recordId, checked)` |
| `cheat_selection_modal.request_close` | `src/modals/CheatSelectionModal.tsx` | `requestClose` |
| `cheat_selection_modal.section` | `src/modals/CheatSelectionModal.tsx` | `(option) => { setSectionKey(String(option.data)); setSearch(""); setPage(0); setExpanded(null); }` |
| `cheat_selection_modal.show_scripts` | `src/modals/CheatSelectionModal.tsx` | `(checked) => { setShowScripts(checked); setPage(0); setExpanded(null); }` |
| `cheat_selection_modal.value` | `src/modals/CheatSelectionModal.tsx` | `(option) => touchValue(recordId, String(option.data))` |
| `debug_details.back` | `src/components/DebugDetails.tsx` | `onBack` |
| `debug_details.refresh` | `src/components/DebugDetails.tsx` | `onRefresh` |
| `debug_details.sessions_next` | `src/components/DebugDetails.tsx` | `() => setSessionPage(stepPage(safePage, apps.length, SESSION_PAGE_SIZE, 1))` |
| `debug_details.sessions_previous` | `src/components/DebugDetails.tsx` | `() => setSessionPage(stepPage(safePage, apps.length, SESSION_PAGE_SIZE, -1))` |
| `game_picker_modal.cancel_back` | `src/modals/GamePickerModal.tsx` | `() => { if (!busyRef.current) onCancel(); }` |
| `game_picker_modal.cancel` | `src/modals/GamePickerModal.tsx` | `() => { if (!busyRef.current) onCancel(); }` |
| `game_picker_modal.game` | `src/modals/GamePickerModal.tsx` | `(option) => setSelection(String(option.data))` |
| `game_picker_modal.use_this_game` | `src/modals/GamePickerModal.tsx` | `() => void submit()` |
| `home_panel.advanced` | `src/components/HomePanel.tsx` | `onAdvanced` |
| `home_panel.cancel_ce_launch` | `src/components/HomePanel.tsx` | `onStopCE` |
| `home_panel.cancel` | `src/components/HomePanel.tsx` | `onCancelInstall` |
| `home_panel.choose_game` | `src/components/HomePanel.tsx` | `onChooseGame` |
| `home_panel.configure_cheats` | `src/components/HomePanel.tsx` | `() => onChooseCheats()` |
| `home_panel.disable_all` | `src/components/HomePanel.tsx` | `onDisableAllCheats` |
| `home_panel.download_and_install_ce` | `src/components/HomePanel.tsx` | `() => onInstall()` |
| `home_panel.load_last_table_cheats` | `src/components/HomePanel.tsx` | `onAutoloadChange` |
| `home_panel.load_table_start_ce` | `src/components/HomePanel.tsx` | `() => onStartRuntime()` |
| `home_panel.manage` | `src/components/HomePanel.tsx` | `onOpenImportedTables` |
| `home_panel.reinstall` | `src/components/HomePanel.tsx` | `onReinstall` |
| `home_panel.retry` | `src/components/HomePanel.tsx` | `onRetrySetupStatus` |
| `home_panel.search` | `src/components/HomePanel.tsx` | `onSearchTable` |
| `home_panel.stop_ce` | `src/components/HomePanel.tsx` | `onStopCE` |
| `home_panel.toggle_cheat` | `src/components/HomePanel.tsx` | `(active) => onTogglePinnedCheat(row.recordId, active)` |
| `imported_tables_modal.back` | `src/modals/ImportedTablesModal.tsx` | `close` |
| `imported_tables_modal.close_2` | `src/modals/ImportedTablesModal.tsx` | `close` |
| `imported_tables_modal.close` | `src/modals/ImportedTablesModal.tsx` | `close` |
| `imported_tables_modal.delete_or_confirm` | `src/modals/ImportedTablesModal.tsx` | `() => { if (armed === table.sha256) { setArmed(null); remove(table.sha256); return; } setFailure(nul...` |
| `imported_tables_modal.filter_by_name_or_digest` | `src/modals/ImportedTablesModal.tsx` | `(event: any) => moveTo(() => { setFilter(String(event.target.value ?? "")); setPage(0); })` |
| `imported_tables_modal.local_file` | `src/modals/ImportedTablesModal.tsx` | `() => { onClose(); onOpenLocalFile(); }` |
| `imported_tables_modal.revoke_or_confirm` | `src/modals/ImportedTablesModal.tsx` | Arm exact-table confirmation, then revoke its captured holders and refresh their state |
| `imported_tables_modal.use` | `src/modals/ImportedTablesModal.tsx` | `() => select(table.sha256)` |
| `pager.next` | `src/components/PagerFooter.tsx` | `() => { turnedRef.current = "next"; onPage(Math.min(pages - 1, page + 1)); }` |
| `pager.previous` | `src/components/PagerFooter.tsx` | `() => { turnedRef.current = "previous"; onPage(Math.max(0, page - 1)); }` |
| `panel.reinstall.cancel` | `src/index.tsx` | `() => confirm.Close()` |
| `panel.reinstall.confirm` | `src/index.tsx` | `() => { confirm.Close(); startManagedSetup(capability, true); }` |
| `panel.setup_failure.cancel` | `src/index.tsx` | `() => dialog.Close()` |
| `panel.setup_failure.close` | `src/index.tsx` | `() => dialog.Close()` |
| `panel.table_refusal.keep` | `src/index.tsx` | `() => { dismiss(); confirm.Close(); }` |
| `panel.table_refusal.stop` | `src/index.tsx` | Capture interaction, then `answerWhenIdle` passes it into `runAction` |
| `panel_row.expand` | `src/components/PanelDensity.tsx` | `() => setExpanded((isOpen) => !isOpen)` |
| `panel_row.help` | `src/components/PanelDensity.tsx` | `() => setHelpOpen((isOpen) => !isOpen)` |
| `provider_catalog.download_again` | `src/providerCatalog.tsx` | `onDownload` |
| `provider_catalog.retry_download` | `src/providerCatalog.tsx` | `onRetryDownload` |
| `provider_catalog.search_query` | `src/providerCatalog.tsx` | `(event: any) => setQuery(String(event.target.value ?? ""))` |
| `provider_catalog.use_saved_copy` | `src/providerCatalog.tsx` | `onUse` |
| `table_acquisition_modal.cancel_back` | `src/modals/TableAcquisitionModal.tsx` | `() => void cancel()` |
| `table_acquisition_modal.cancel` | `src/modals/TableAcquisitionModal.tsx` | `() => void cancel()` |
| `table_acquisition_modal.import_selected_table` | `src/modals/TableAcquisitionModal.tsx` | `() => void complete(selectedMember?.path ?? null)` |
| `table_acquisition_modal.password` | `src/modals/TableAcquisitionModal.tsx` | `(event: any) => setPassword(String(event.target.value ?? ""))` |
| `table_acquisition_modal.retry_status` | `src/modals/TableAcquisitionModal.tsx` | `() => { setReadError(null); setReadAttempt((attempt) => attempt + 1); }` |
| `table_acquisition_modal.table_in_downloaded_archive` | `src/modals/TableAcquisitionModal.tsx` | `(option) => { setMemberPath(String(option.data)); setPassword(""); }` |
| `table_code_modal.back_to_the_list` | `src/modals/TableCodeModal.tsx` | `closeSection` |
| `table_code_modal.back` | `src/modals/TableCodeModal.tsx` | `onBack` |
| `table_code_modal.cancel_back` | `src/modals/TableCodeModal.tsx` | `onBack` |
| `table_code_modal.close_section` | `src/modals/TableCodeModal.tsx` | `closeSection` |
| `table_code_modal.read_section` | `src/modals/TableCodeModal.tsx` | `() => void open(entry)` |
| `table_review_modal.abort` | `src/modals/TableReviewModal.tsx` | `() => void abort()` |
| `table_review_modal.cancel_back` | `src/modals/TableReviewModal.tsx` | `() => { if (!busyRef.current && !abortingRef.current) onCancel(); }` |
| `table_review_modal.cancel` | `src/modals/TableReviewModal.tsx` | `() => { if (!busyRef.current && !abortingRef.current) onCancel(); }` |
| `table_review_modal.game_process` | `src/modals/TableReviewModal.tsx` | `(option) => { const next = String(option.data); setSelector(next); if (next !== CUSTOM_PROCESS) setC...` |
| `table_review_modal.look_inside_this_table` | `src/modals/TableReviewModal.tsx` | `openCode` |
| `table_review_modal.look_inside` | `src/modals/TableReviewModal.tsx` | `openCode` |
| `table_review_modal.on_rescan_2` | `src/modals/TableReviewModal.tsx` | `onRescan` |
| `table_review_modal.process_exe_basename` | `src/modals/TableReviewModal.tsx` | `(event: any) => setCustomProcess(String(event.target.value ?? ""))` |
| `table_review_modal.use_this_table` | `src/modals/TableReviewModal.tsx` | `() => void use()` |
| `table_search_modal.cancel` | `src/modals/TableSearchModal.tsx` | `cancel` |
| `table_search_modal.close` | `src/modals/TableSearchModal.tsx` | `cancel` |
