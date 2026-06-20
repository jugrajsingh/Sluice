# Sluice — Commercial License

Sluice is published under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**.
A copy of that license is in the `LICENSE` file at the repository root.

This document describes the **commercial license offer** — a separate, per-deal agreement for
organizations whose use-case is incompatible with the AGPL.

---

## When do you need a commercial license?

You need a commercial license if **any** of the following apply:

1. **You run Sluice as a managed or hosted service** (SaaS, PaaS, MLaaS, inference API, etc.)
   and you do not want to comply with AGPL §13's requirement to make the complete corresponding
   source code of the modified version available to users who interact with it over a network.

2. **You embed or redistribute Sluice** as part of a proprietary product without releasing your
   product's source under the AGPL.

3. **Your legal team or enterprise procurement policy** requires a non-copyleft license,
   commercial warranties, or an indemnification agreement.

If you are building an internal tool that only your organization uses and you are prepared to share
source modifications back under the AGPL, you do **not** need a commercial license.

---

## What the commercial license grants

A commercial license agreement grants you:

- The right to **use, modify, and distribute Sluice** (and your modifications) as part of a
  proprietary or hosted product **without the AGPL source-disclosure obligations**.
- The right to **run Sluice as a managed or hosted service** — including SaaS/PaaS offerings —
  without triggering AGPL §13.
- **No copyleft propagation to your inference models.** Sluice orchestrates inference models in
  separate processes; the AGPL does not and never did extend to those models. A commercial license
  makes this contractually explicit and removes any ambiguity for your legal team.
- (Optional, per deal) **Enterprise support**, SLA guarantees, and **warranty coverage** beyond
  the "as-is" disclaimer of the open-source license.
- (Optional, per deal) **Indemnification** against intellectual-property claims related to the
  licensed code.

---

## What the commercial license does NOT grant

- A commercial license does not relicense any third-party dependencies. Each dependency carries
  its own license; it is your responsibility to ensure compatibility.
- A commercial license does not entitle you to code contributions from the maintainer beyond what
  is scoped in the individual agreement.

---

## Scope note — inference models are always yours

Sluice communicates with inference models through subprocess or network boundaries (separate
processes). Under standard AGPL interpretation, a work that merely uses a separately distributed
program over a process boundary is **not** a derivative work of that program. A commercial license
makes this contractual: **your proprietary models, weights, and serving code remain entirely
yours** and are not subject to any Sluice licensing requirement.

---

## How to obtain a commercial license

Commercial license terms are finalized on a per-deal basis. To start a conversation:

**Email:** jugrajskhalsa@gmail.com
**Subject line:** `Sluice commercial license inquiry — <your organization>`

Please include a brief description of your intended use-case and approximate deployment scale.
You will receive a response within a few business days.

---

## Enterprise support and professional services

Available separately or bundled with a commercial license:

- Dedicated support channel with response-time SLAs
- Architecture reviews and onboarding assistance
- Custom driver development (new queue, store, or compute backends)
- Security disclosures and patch coordination under NDA

Inquire at the address above.

---

*This document describes a commercial license offer. It is not itself a legally binding contract.
Binding terms are established only by a signed license agreement between the licensee and
Jugraj Singh.*
