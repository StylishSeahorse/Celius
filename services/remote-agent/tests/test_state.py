from celius_agent.state import SessionState, StateStore


def test_outbox_survives_restart_and_drops_acked_events(tmp_path):
    store = StateStore(tmp_path)
    session = SessionState(id="s1", status="open")
    store.save_session(session)
    for n in range(5):
        store.append(session, "terminal.output", {"n": n})
    store.ack(session, 2)
    assert [e["seq"] for e in session.pending] == [3, 4, 5]

    # A new process sees exactly the unacknowledged events, and continues numbering.
    reloaded = StateStore(tmp_path).load_sessions()["s1"]
    assert [e["seq"] for e in reloaded.pending] == [3, 4, 5]
    assert reloaded.next_seq == 6
    assert reloaded.acked == 2


def test_torn_last_line_is_ignored(tmp_path):
    store = StateStore(tmp_path)
    session = SessionState(id="s1")
    store.save_session(session)
    store.append(session, "a", {})
    with (tmp_path / "outbox" / "s1.jsonl").open("a") as handle:
        handle.write('{"seq": 2, "type": "b", "da')  # crash mid-write
    reloaded = StateStore(tmp_path).load_sessions()["s1"]
    assert [e["seq"] for e in reloaded.pending] == [1]
    assert reloaded.next_seq == 2


def test_server_sequence_is_adopted_after_reinstall(tmp_path):
    store = StateStore(tmp_path)
    session = SessionState(id="s1")
    store.adopt_server_seq(session, 41)
    assert session.next_seq == 42
    assert store.append(session, "x", {})["seq"] == 42


def test_outbox_is_compacted(tmp_path):
    store = StateStore(tmp_path)
    session = SessionState(id="s1")
    store.save_session(session)
    for _ in range(300):
        store.append(session, "x", {})
    store.ack(session, 250)
    lines = (tmp_path / "outbox" / "s1.jsonl").read_text().splitlines()
    assert len(lines) == 50


def test_journal_reports_unfinished_commands(tmp_path):
    store = StateStore(tmp_path)
    store.journal_put("c-1", {"id": "c-1", "kind": "tool.execute", "state": "started"})
    store.journal_put("c-2", {"id": "c-2", "kind": "tool.execute", "state": "done"})
    assert [e["id"] for e in StateStore(tmp_path).journal_unfinished()] == ["c-1"]
    assert store.journal_get("c-2")["state"] == "done"


def test_oversized_events_are_trimmed_to_fit_a_frame(tmp_path):
    import json

    from celius_agent.state import MAX_EVENT_BYTES

    store = StateStore(tmp_path)
    session = SessionState(id="s1")
    store.save_session(session)
    huge = "x" * (MAX_EVENT_BYTES + 10)
    event = store.append(
        session,
        "command.completed",
        {"command_id": "c1", "result": {"content": huge, "data": {"content_b64": huge}}},
    )
    assert len(json.dumps(event)) < MAX_EVENT_BYTES
    assert event["data"]["command_id"] == "c1"
    assert event["data"]["result"]["data"]["content_b64"].startswith("[removed")
    reloaded = StateStore(tmp_path).load_sessions()["s1"]
    assert reloaded.pending[0]["data"]["command_id"] == "c1"
