"""Stage 8: notification delivery and the human-review queue.

    models.py    what a notification is, and what a delivery attempt returned
    provider.py  the interface, one real implementation, one fake
    service.py   eligibility, rendering, delivery, retry, and the review queue

Stage 6 decides. Stage 7 explains. Stage 8 delivers and queues review.
Stage 8 cannot change Stage 6. Stage 8 executes no business action.

What this package does not contain
----------------------------------
No severity. No routing rule. No threshold. Nothing here decides who should be
told - it asks Stage 6 and does what it says. The two eligibility questions are
single SQL predicates over columns Stage 6 owns, and the database refuses a
notification or review row whose snapshot does not match.

Stage 8 also ends where it ends. There is no refund, no order change, no CRM
update, no ticket, no approval. The last thing that happens is a row saying a
notification was delivered, or a row saying a human needs to look. What anyone
does about it is a later stage's problem.
"""
