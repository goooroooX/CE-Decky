import type { ManagedCECapability, ManagedCECompletion, ManagedCEInstallStatus, PluginStatus } from "./types";
import { describeError } from "./errors";
import { logUi, logUiFailure } from "./supportLog";

type Operation = ManagedCEInstallStatus;
const active = (op: Operation) => ["downloading", "extracting", "verifying"].includes(op.state);
const resumable = (op: Operation) => active(op) || op.state === "completed";

interface Dependencies {
  capability: () => Promise<ManagedCECapability>;
  status: () => Promise<PluginStatus>;
  start: (force: boolean) => Promise<Operation>;
  poll: (id: string) => Promise<Operation>;
  cancel: (id: string) => Promise<Operation>;
  complete: (id: string) => Promise<ManagedCECompletion>;
  publish: (op: Operation | null) => void;
  completed: (receipt: ManagedCECompletion, cancelled: boolean) => void;
  signal: AbortSignal;
}

/** One mounted frontend owner. Cancel interrupts a read, never creates a second
 * monitor or completion consumer. Superseded RPC replies have no side effects. */
export class ManagedSetupOwner {
  operation: Operation | null = null;
  private interrupts = new Set<() => void>();
  private completion: { operationId: string; receipt: ManagedCECompletion } | null = null;
  private cancellation: {
    receipt: Promise<Operation>;
    resolve: (state: string) => void;
    reject: (cause: unknown) => void;
    received: boolean;
  } | null = null;

  constructor(private deps: Dependencies) {}

  private publish(op: Operation | null) {
    if (op?.operation_id !== this.operation?.operation_id || op?.state !== this.operation?.state) {
      logUi("managed_setup.observed", { operation_id: op?.operation_id ?? this.operation?.operation_id, state: op?.state ?? "consumed" });
    }
    this.operation = op;
    if (!this.deps.signal.aborted) this.deps.publish(op);
  }

  private async complete(operationId: string): Promise<ManagedCECompletion> {
    if (this.completion?.operationId === operationId) return this.completion.receipt;
    const receipt = await this.deps.complete(operationId);
    this.completion = { operationId, receipt };
    return receipt;
  }

  private handoff(previous: Operation, capability: ManagedCECapability): boolean {
    const next = capability.operation;
    if (!next || next.operation_id === previous.operation_id) return false;
    logUi("managed_setup.owner_handoff", { previous: previous.operation_id, previous_state: previous.state, previous_error: previous.error, operation_id: next.operation_id });
    this.publish(next);
    return true;
  }

  cancel(): Promise<string> {
    if (this.deps.signal.aborted || !this.operation || !active(this.operation) || this.cancellation) {
      return Promise.reject(new Error("Managed setup is not cancellable."));
    }
    return new Promise((resolve, reject) => {
      // Capture the exact operation before sending, and attach a rejection
      // observer now even if an outstanding read has not yielded yet.
      const receipt = this.deps.cancel(this.operation!.operation_id);
      void receipt.catch(() => undefined);
      this.cancellation = { receipt, resolve, reject, received: false };
      for (const interrupt of this.interrupts) interrupt();
    });
  }

  // Only read-only requests may abandon a deadline. Decky callables cannot be
  // cancelled by this race; mutations below retain their actual promises.
  private async read<T>(request: Promise<T>, interruptible = false): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let abort!: () => void;
    let interrupt: (() => void) | undefined;
    try {
      if (this.deps.signal.aborted) throw new Error("Managed setup observer closed.");
      return await Promise.race([
        request,
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error("Managed setup did not answer in time.")), 8000);
          abort = () => reject(new Error("Managed setup observer closed."));
          this.deps.signal.addEventListener("abort", abort, { once: true });
        }),
        ...(interruptible ? [new Promise<never>((_, reject) => {
          interrupt = () => reject(new Error("Managed setup cancellation requested."));
          this.interrupts.add(interrupt);
        })] : []),
      ]);
    } finally {
      clearTimeout(timer);
      this.deps.signal.removeEventListener("abort", abort);
      if (interrupt) this.interrupts.delete(interrupt);
    }
  }

  private async pause(ms: number) {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await this.read(new Promise<void>((resolve) => { timer = setTimeout(resolve, ms); }), true);
    } catch (cause) {
      if (!this.cancellation && !this.deps.signal.aborted) throw cause;
    } finally { clearTimeout(timer); }
  }

  private async reconcile(cause: unknown, previousTerminalId?: string): Promise<Operation | null> {
    // Unavailable truth is not terminal truth. Keep this owner, with capped
    // backoff, until the backend answers or the frontend deliberately unmounts.
    let attempts = 0;
    while (!this.deps.signal.aborted) {
      if (this.cancellation && !this.cancellation.received) return this.operation;
      if (this.operation && this.completion?.operationId === this.operation.operation_id) {
        return { ...this.operation, state: "completed", installed: this.completion.receipt };
      }
      try {
        const capability = await this.read(this.deps.capability(), !this.cancellation?.received);
        if (this.cancellation && !this.cancellation.received) return this.operation;
        const next = capability.operation;
        if (next) {
          // A start refused before creating anything leaves the previous
          // failed/cancelled operation visible. Its old error is not this press.
          if (!this.operation && next.operation_id === previousTerminalId && !resumable(next)) throw cause;
          if (this.operation && next.operation_id !== this.operation.operation_id) {
            logUi("managed_setup.owner_handoff", { previous: this.operation.operation_id, operation_id: next.operation_id });
          }
          return next;
        }
        if (!this.operation) throw cause;
        const operation = this.operation;
        let receipt: ManagedCECompletion;
        try {
          // This exact-ID call is idempotent even after another frontend has
          // consumed the operation. Registration heuristics cannot prove it.
          receipt = await this.complete(operation.operation_id);
        } catch (failure) {
          if (this.cancellation && !this.cancellation.received) return operation;
          // The service explicitly refuses an ID for which it has neither a
          // live reservation nor a retained completion receipt. A transport
          // error proves neither and must keep reconciling.
          if (describeError(failure) !== "managed CE setup changed; refresh before completing it") throw failure;
          return { ...operation, state: "failed", progress: null, error: describeError(cause) };
        }
        logUi("managed_setup.completion_reconciled", { operation_id: operation.operation_id, completed_now: receipt.completed_now });
        // Let the main owner consume pending cancellation first, then finish
        // from this cached exact receipt without repeating the mutation.
        return { ...operation, state: "completed", installed: receipt };
      } catch (failure) {
        if (this.cancellation && !this.cancellation.received) return this.operation;
        if (!this.operation && failure === cause) throw cause;
        attempts += 1;
        if (attempts === 1 || attempts === 3) logUiFailure("managed_setup.reconciliation_retry", failure, { operation_id: this.operation?.operation_id, attempt: attempts });
        await this.pause(Math.min(500 * attempts, 5000));
      }
    }
    throw new Error("Managed setup observer closed.");
  }

  async run(force: boolean, initial: ManagedCECapability): Promise<void> {
    let retry = 0;
    try {
      if (this.deps.signal.aborted) return;
      let first = initial.operation && resumable(initial.operation) ? initial.operation : null;
      if (!first) {
        const previousTerminalId = initial.operation?.operation_id;
        try { first = await this.deps.start(force); }
        catch (cause) {
          logUiFailure("managed_setup.start_receipt_lost", cause);
          first = await this.reconcile(cause, previousTerminalId);
        }
      }
      this.publish(first);
      while (this.operation && !this.deps.signal.aborted) {
        let cancellation = this.cancellation;
        if (cancellation && !cancellation.received) {
          cancellation.received = true;
          try {
            let next: Operation | null;
            try { next = await cancellation.receipt; }
            catch (cause) {
              logUiFailure("managed_setup.cancel_receipt_lost", cause, { operation_id: this.operation?.operation_id });
              next = await this.reconcile(cause);
              if (next && (active(next) || next.operation_id !== this.operation?.operation_id)) {
                this.publish(next);
                cancellation.reject(cause);
                this.cancellation = null;
                continue;
              }
            }
            this.publish(next);
          } catch (cause) {
            cancellation.reject(cause);
            this.cancellation = null;
            throw cause;
          }
        }
        const operation = this.operation;
        if (!operation) {
          cancellation?.resolve("completed");
          this.cancellation = null;
          return;
        }
        if (!active(operation)) {
          if (operation.state === "completed") {
            let refreshed: ManagedCECapability | null = null;
            try {
              const receipt = await this.complete(operation.operation_id);
              this.publish(null);
              // These reads cannot undo the durable completion receipt.
              const [, capability] = await Promise.allSettled([this.read(this.deps.status()), this.read(this.deps.capability())]);
              this.deps.completed(receipt, Boolean(cancellation));
              if (capability.status === "fulfilled") refreshed = capability.value;
            } catch (cause) {
              retry += 1;
              if (retry === 1 || retry === 3) logUiFailure("managed_setup.completion_retry", cause, { operation_id: operation.operation_id, attempt: retry });
              const next = await this.reconcile(cause);
              if (next && next.operation_id !== operation.operation_id) {
                // Cancel already returned completed for this exact predecessor.
                // A newer operation cannot revoke that terminal receipt.
                cancellation?.resolve("completed");
                this.cancellation = null;
              }
              this.publish(next);
              if (next) { await this.pause(Math.min(500 * retry, 5000)); continue; }
            }
            cancellation?.resolve("completed");
            this.cancellation = null;
            if (refreshed && this.handoff(operation, refreshed)) {
              retry = 0;
              continue;
            }
            return;
          }
          // A known cancellation stays cancelled even if this advisory read
          // fails. No failed refresh may relabel a durable terminal result.
          if (operation.state === "cancelled") {
            cancellation?.resolve("cancelled");
            this.cancellation = null;
          }
          const capability = await this.read(this.deps.capability()).catch(() => null);
          if (capability && this.handoff(operation, capability)) {
            if (operation.state === "cancelled") cancellation?.resolve("cancelled");
            else cancellation?.reject(new Error(operation.error || operation.message));
            this.cancellation = null;
            retry = 0;
            continue;
          }
          this.publish(operation);
          if (operation.state === "cancelled") {
            cancellation?.resolve("cancelled");
            this.cancellation = null;
            return;
          }
          throw new Error(operation.error || operation.message || "Cheat Engine setup did not complete.");
        }
        if (cancellation) {
          cancellation.reject(new Error("Cheat Engine setup is still active; cancellation was not confirmed."));
          this.cancellation = null;
        }
        await this.pause(Math.min(500 * (retry + 1), 5000));
        if (this.cancellation || this.deps.signal.aborted) continue;
        try {
          const next = await this.read(this.deps.poll(operation.operation_id), true);
          if (this.cancellation) continue;
          this.publish(next);
          retry = 0;
        } catch (cause) {
          if (this.cancellation || this.deps.signal.aborted) continue;
          retry += 1;
          if (retry === 1 || retry === 3) logUiFailure("managed_setup.poll_retry", cause, { operation_id: operation.operation_id, attempt: retry });
          this.publish(await this.reconcile(cause));
        }
      }
    } catch (cause) {
      this.cancellation?.reject(cause);
      this.cancellation = null;
      if (!this.deps.signal.aborted) throw cause;
    } finally {
      this.cancellation?.reject(new Error("Managed setup observer ended before cancellation was confirmed."));
      this.cancellation = null;
    }
  }
}
