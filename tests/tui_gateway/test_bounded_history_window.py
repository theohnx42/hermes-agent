from tui_gateway.server import DESKTOP_HISTORY_WINDOW_MAX, _bounded_history_window


def test_200k_event_lineage_hydrates_only_the_exact_tip_window():
    history = [
        {"id": index + 1, "role": "user" if index % 2 == 0 else "assistant", "content": f"event-{index}"}
        for index in range(200_000)
    ]

    window, cursor = _bounded_history_window(history, DESKTOP_HISTORY_WINDOW_MAX)

    assert len(window) == DESKTOP_HISTORY_WINDOW_MAX
    assert window[0]["content"] == "event-199500"
    assert window[-1] is history[-1]
    assert cursor == {
        "has_more_before": True,
        "limit": DESKTOP_HISTORY_WINDOW_MAX,
        "start": 199_500,
        "total": 200_000,
    }
    assert len(history) == 200_000


def test_adjacent_recovery_page_has_no_duplicate_boundary_event():
    history = [{"id": index + 1, "role": "user", "content": f"event-{index}"} for index in range(1_200)]

    tip, cursor = _bounded_history_window(history, 500)
    older = history[cursor["start"] - 500 : cursor["start"]]

    assert older[-1]["id"] + 1 == tip[0]["id"]
    assert {row["id"] for row in older}.isdisjoint(row["id"] for row in tip)


def test_legacy_clients_keep_full_history():
    history = [{"id": index, "role": "user", "content": str(index)} for index in range(750)]

    full, cursor = _bounded_history_window(history)

    assert full is history
    assert cursor is None
