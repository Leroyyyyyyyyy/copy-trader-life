# Tradelife L5: checkpoint, SQL saver, and idempotent side effects

## Conclusion

Workflow persistence and side-effect safety are separate responsibilities.

- Native and LangGraph workflows may rerun a node when it did not return successfully and advance its checkpoint.
- If `commit` changes the account and the process crashes before the checkpoint advances, recovery enters `commit` again.
- A checkpoint records workflow progress; it does not own `plans`, `submit_count`, trading state, or risk rules.
- `SimulationGateway` must enforce idempotency with a stable key and persisted receipt. Repeating the same key and payload returns the prior receipt without applying the account mutation again; reusing the key with a different payload is rejected.
- Splitting `commit` into smaller nodes reduces repeated computation but cannot remove the crash window between an external side effect and recording progress. A check-then-act split is also unsafe under concurrency.
- A SQLite or PostgreSQL LangGraph saver is still a durable checkpoint implementation, not a replacement for checkpoints or receipts. It enables cross-process workflow recovery but does not provide exactly-once external effects.
- Recommended separation: SQL saver/checkpoint tables restore graph progress; commit receipt tables protect side effects. They may share a database but have different responsibilities.
- Only when account mutation, receipt insertion, and progress update participate in one atomic transaction can a transaction jointly protect them. Real external exchanges normally cannot participate in the local SQL transaction, so exchange/client idempotency keys remain necessary.

## Project anchors

- `src/lab/workflows/nodes.py`: shared nodes and `commit_merged`
- `src/lab/workflows/native.py`: SQLite-backed native checkpoint pointer
- `src/lab/workflows/langgraph.py`: current `InMemorySaver` implementation
- `src/lab/execution/simulation.py`: receipt-based idempotency
- `tests/lab/test_l5_workflows.py`: crash and resume acceptance tests
- `handoff.md`: L5 learning notes
