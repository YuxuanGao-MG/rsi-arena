# Checks for the reader

Node, no dependencies, no network. Each stubs the DOM and PostgREST and drives
the real modules, because the alternative is reasoning about a page nobody has
loaded — which is how the site came to show the best of the worst five hundred
windows under a heading that said "Where it won".

    node web/tests/router.mjs     # all 13 routes: title, breadcrumb, aria-live, and
                                  # that a superseded route never paints over the current one
    node web/tests/checks.mjs     # empty states, including the two tables that do not exist yet
    node web/tests/arch.mjs       # archive.js against loop/archive.py: same frontier, same win counts
    node web/tests/a11y.mjs       # headings, table scope, link names, tabindex, duplicate ids
    node web/tests/vote.mjs       # the reveal is not in the DOM before the vote

`fixtures.json` is generated from the real `runs/` directories, so the shapes are
the shapes the site will actually meet. They are not wired into `pytest`: that
suite is Python and offline by rule, and these need node.
