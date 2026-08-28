# Gathered Scenes Zine v10

## Community fork

This v10 visual workflow is maintained by `taa211`.

Instead of applying a fixed filter, v10 first identifies the meaningful visual relationship in each photograph, chooses a mechanism specific to that source, and then renders a paper artwork with visible source causality and stronger visual impact.

## What v10 adds

- Visual decision card: subject, relationships, canvas direction, transformation, and eye path are decided before rendering.
- Source causality: major shapes, colors, and spatial actions must trace back to the photograph.
- Direct rendering: the decision card goes straight to rendering without a lossy `final_prompt` middle stage.
- Quality gates: checks for forced connections, reusable templates, timid composition, and loss of source identity.

## Use

Install `skills/scenes-gathered-zine-v10/` into your Codex Skills directory and call:

```text
$scenes-gathered-zine-v10
```

The browser batch runner and offline tests are in [`v10/`](v10/). See [`V10-MODIFICATIONS.md`](V10-MODIFICATIONS.md) for the exact change list.

## Source and license

This project is modified from [Zeejay0's upstream repository](https://github.com/Zeejay0/gathered-scenes-zine-skill). The upstream files remain in this repository; this homepage documents only this fork's v10 work.

This repository follows the upstream [Personal Non-Commercial License](LICENSE). Keep copyright, attribution, source, and license notices intact. Commercial use requires written permission from the upstream author.
