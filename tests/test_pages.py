"""What a person sees: copy and page state that tell the truth about the event."""

from .conftest import post_form


def test_after_the_close_the_edit_page_says_final_and_has_no_form(participant):
    page = participant.get("/projects/prj_01/edit")
    assert page.status_code == 200
    assert "can no longer change" in page.text
    assert 'name="title"' not in page.text
    project = participant.get("/projects/prj_01").text
    assert "Add one" not in project


def test_domain_errors_read_as_sentences_on_a_page(participant):
    r = post_form(participant, "/projects/prj_01/edit", {"title": "Late", "action": "submit"})
    assert r.status_code == 409
    # First letter up, the event name left alone, the time printed the way pages print times.
    assert "Submissions for Sample Hack 2026 closed at 2026-03-01 18:00 UTC; entries are final." in r.text


def test_reviews_left_out_of_the_ranking_are_explained(organizer):
    assert "5 more reviews score an entry dropped as a duplicate" in organizer.get("/events/evt_01/manage").text
    assert "5 more reviews score an entry dropped as a duplicate" in organizer.get("/events/evt_01/manage/results").text


def test_an_all_tie_ranking_says_so_once_instead_of_in_every_row(organizer):
    page = organizer.get("/events/evt_01/manage/results").text
    assert "every neighbouring pair is a tie" in page
    assert "<th class=\"num\">Beats next</th>" not in page


def test_fixture_placeholder_summary_is_not_printed(anon):
    assert "One line of what it does." not in anon.get("/projects/prj_01").text


def test_demo_login_page_lists_the_demo_accounts(anon):
    page = anon.get("/login").text
    assert "diego.herrera@example.org" in page and "plumb-demo-2026" in page
