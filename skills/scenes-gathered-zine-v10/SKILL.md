---
name: scenes-gathered-zine-v10
description: Use the v10 visual-decision-card workflow to turn a user photo into a source-specific gathered-scenes paper artwork.
---

# Scenes Gathered Zine v10

This is a community v10 extension of Zeejay0's `scenes-gathered-zine-v1-3` skill.
Keep the source photograph as the factual anchor, but choose a source-derived
visual mechanism before rendering. The mechanism should be bold, specific to
the photograph, and should not become a reusable photo-on-paper template.

## Standard flow

1. Read the uploaded photograph and produce a visual decision card using
   `v10/plans/gpt-direct-v10-visual-decision-card.txt`.
2. Validate every required field and repair the card once if needed.
3. Render directly from that card using
   `v10/plans/gpt-direct-v10-render-from-card.txt`. Do not create a separate
   `final_prompt` stage.
4. Preserve source causality: the hero transformation, eye path, quiet field,
   and structural color must visibly come from the source image.

For the browser batch runner, see `v10/scripts/v10_visual_batch.py`. Its offline
contract tests are in `v10/scripts/test_v10_visual_batch.py`.

## Attribution and license

This folder is a modified version of the upstream gathered-scenes workflow.
Keep the repository `LICENSE` and upstream attribution when redistributing it.
This fork remains subject to the upstream personal, non-commercial license.
