# Reviewed third-party distribution

The complete history of `0x3bc4924fb2ee3adf6f2c38b8cb40782780e4bebb`
contained a credit in transaction
`0x28c537dff9efb992ac8301cfbe28b89d055fb7cfaf34354f57442336a5a9a3fe`,
block 21186465. A Swap elsewhere in the receipt prevented the ordinary
incoming-only credit rule from explaining this inventory flag.

The preserved input and 509 receipt logs establish the following observations:

- External sender `0xad7c8ae8f80d791d473a9611af581536d711baf2` supplied
  9.618582 native USDC to distributor
  `0x69fc5e25a07bd1e185d0cd24f844bad74af8ba44`.
- The input encodes that cost and an array of 500 distinct recipients. The
  distributor pays the V4 manager and receives the purchased token.
- It emits 500 token transfers of exactly `10**18` raw units, one to each
  recipient in that input array. The researched wallet is one such recipient.
- The wallet has no observed cash payment or token outflow in the receipt.
  Its complete saved snapshot contains no trade or disposal of this token.

This does not establish that the recipient identities are independent, that
the sender is unrelated to them, or that any token has a particular value.
It is not an inferred BUY, win, realized profit, loss or copy signal.

`copy_distribution_research.py` recognizes **only this reviewed transaction**,
with its exact block hash, payer, distributor, native value and pinned SHA-256
digests of the input and complete log array. It also requires receipt success,
the expected wallet credit, no wallet cash flow, and no observed disposal or
trading of the token anywhere in the complete snapshot. Other transactions,
truncated/changed evidence, incomplete histories or subsequently used tokens
remain unresolved. The presence of many recipients is not a general exemption.

The isolated replay retains the original flagged payload and adds an explicit
`REVIEWED_THIRD_PARTY_DISTRIBUTION` annotation to the research copy. It does not
change the source database, PnL, safety scoring, identity clustering, production
promotion, paper selection or live authority. No API request is needed.
