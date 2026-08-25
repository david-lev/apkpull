from apkpull import uidump

from .helpers import make_dump, make_icon_dump


def test_find_button_returns_center_of_bounds():
    dump = make_dump(("Install", (100, 200, 300, 260)))
    assert uidump.find_button(dump, "Install") == (200, 230)


def test_find_button_missing_returns_none():
    dump = make_dump(("Open", (0, 0, 10, 10)))
    assert uidump.find_button(dump, "Install") is None


def test_find_button_requires_exact_match():
    dump = make_dump(("Reinstall", (0, 0, 10, 10)))
    assert uidump.find_button(dump, "Install") is None


def test_contains_text():
    dump = make_dump(("You're offline", (0, 0, 10, 10)))
    assert uidump.contains_text(dump, "You're offline")
    assert not uidump.contains_text(dump, "Install")


def test_unescapes_xml_entities():
    dump = '<hierarchy><node text="You&apos;re offline" bounds="[0,0][10,10]"/></hierarchy>'
    assert uidump.contains_text(dump, "You're offline")


def test_contains_any_currency_amount_detects_price_suffix():
    dump = make_dump(("$4.99", (0, 0, 10, 10)))
    assert uidump.contains_any_currency_amount(dump, ("$", "₪"))


def test_contains_any_currency_amount_detects_price_prefix():
    dump = make_dump(("₪17.90", (0, 0, 10, 10)))
    assert uidump.contains_any_currency_amount(dump, ("$", "₪"))


def test_contains_any_currency_amount_ignores_plain_numbers():
    dump = make_dump(("v4.99.1", (0, 0, 10, 10)))
    assert not uidump.contains_any_currency_amount(dump, ("$", "₪"))


def test_iter_nodes_skips_nodes_without_text_or_bounds():
    dump = '<hierarchy><node bounds="[0,0][10,10]"/><node text="X"/></hierarchy>'
    assert list(uidump.iter_nodes(dump)) == []


def test_find_text_near_content_desc_pairs_icon_with_its_message():
    dump = make_icon_dump("Warning", "This item isn't available in your country.")
    assert (
        uidump.find_text_near_content_desc(dump, "Warning")
        == "This item isn't available in your country."
    )


def test_find_text_near_content_desc_picks_nearest_when_multiple_texts_present():
    dump = (
        '<?xml version="1.0"?><hierarchy rotation="0">'
        '<node text="" content-desc="Warning" bounds="[36,1156][84,1204]"/>'
        '<node text="Far away, unrelated" bounds="[0,0][10,10]"/>'
        '<node text="Right next to the icon" bounds="[132,1150][881,1210]"/>'
        "</hierarchy>"
    )
    assert (
        uidump.find_text_near_content_desc(dump, "Warning") == "Right next to the icon"
    )


def test_find_text_near_content_desc_returns_none_without_matching_icon():
    dump = make_dump(("Some text", (0, 0, 10, 10)))
    assert uidump.find_text_near_content_desc(dump, "Warning") is None


def test_find_text_near_content_desc_returns_none_without_any_text():
    dump = '<?xml version="1.0"?><hierarchy rotation="0"><node text="" content-desc="Warning" bounds="[36,1156][84,1204]"/></hierarchy>'
    assert uidump.find_text_near_content_desc(dump, "Warning") is None
