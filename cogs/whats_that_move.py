"""'What's That Move?' — Pokemon-silhouette-style frame-data quiz.

`/whats-that-move` picks a random move from `frame_data.T8_KEY_MOVES`,
renders a Pillow card with the character portrait + notation + move
name + an obscured answer placeholder, and posts it with four
multiple-choice buttons (correct + three close distractors).

First click reveals the answer for everyone (the asker doesn't have a
monopoly — community guessing is the fun part). The card re-renders
with the answer in big colour-graded text: green for safe, amber for
mid-minus, red for launch-punishable.

Quiz state is per-message, lives in the View instance; no DB table.
The View has a 5-minute timeout, after which clicks fall through to
discord.py's default "interaction failed" toast — acceptable for a
casual mini-game.
"""
from __future__ import annotations

import logging
import random
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

import frame_data
import tournament_render
from view_util import ErrorHandledView, handle_app_command_error

log = logging.getLogger(__name__)

QUIZ_TIMEOUT_SECONDS = 300


def _generate_choices(correct: int) -> list[int]:
    """Return four shuffled answer choices: the correct value plus
    three close-but-wrong distractors. Offsets bias toward small
    deltas so the quiz tests the player's knowledge of the *safety
    bracket* (safe / mid-minus / launch) rather than rewarding wild
    guesses."""
    offsets = [-8, -5, -3, -2, +2, +3, +5, +8]
    random.shuffle(offsets)
    choices: list[int] = [correct]
    seen = {correct}
    for delta in offsets:
        cand = correct + delta
        if cand in seen:
            continue
        seen.add(cand)
        choices.append(cand)
        if len(choices) == 4:
            break
    # Safety net if offsets collide hard (shouldn't happen with that pool).
    while len(choices) < 4:
        cand = correct + random.choice([-15, -12, +6, +10])
        if cand not in seen:
            seen.add(cand)
            choices.append(cand)
    random.shuffle(choices)
    return choices


def _safety_color(frames: int) -> tuple[int, int, int]:
    """Same brackets the renderer uses, mirrored here so the embed
    border matches the reveal card's tint."""
    if frames >= -9:
        return (95, 180, 120)
    if frames >= -13:
        return (245, 180, 95)
    return (220, 60, 60)


class WhatsThatMoveView(ErrorHandledView):
    """Dynamic-button view: four answer choices generated per-quiz so
    the value lives on the button itself rather than in some external
    map. Not persistent — quizzes are ephemeral one-shots and the
    state would be hard to recover after a bot restart anyway."""

    def __init__(self, move: frame_data.Move, choices: Iterable[int]):
        super().__init__(timeout=QUIZ_TIMEOUT_SECONDS)
        self.move = move
        self.answered = False
        for value in choices:
            button = discord.ui.Button(
                label=f"{value:+d}",
                style=discord.ButtonStyle.secondary,
            )
            button.callback = self._make_callback(value)
            self.add_item(button)

    def _make_callback(self, choice_value: int):
        async def callback(interaction: discord.Interaction) -> None:
            if self.answered:
                await interaction.response.send_message(
                    "Already answered — start a new quiz with `/whats-that-move`.",
                    ephemeral=True, delete_after=8,
                )
                return
            self.answered = True
            correct = self.move.frames_on_block
            won = choice_value == correct

            # Re-render the card with the answer revealed. Same canvas
            # dimensions so the swap reads as a flip rather than a
            # whole new message.
            try:
                buf = await tournament_render.render_whats_that_move_card(
                    character=self.move.character,
                    notation=self.move.notation,
                    move_name=self.move.name,
                    revealed_frames=correct,
                )
            except Exception:
                log.exception("[whats-that-move] reveal render failed")
                await interaction.response.send_message(
                    "Couldn't render the reveal — try a fresh quiz.",
                    ephemeral=True, delete_after=8,
                )
                return

            # Recolour buttons so the result reads at a glance.
            for child in self.children:
                if not isinstance(child, discord.ui.Button):
                    continue
                child.disabled = True
                try:
                    label_value = int(child.label)
                except (TypeError, ValueError):
                    continue
                if label_value == correct:
                    child.style = discord.ButtonStyle.success
                elif label_value == choice_value and not won:
                    child.style = discord.ButtonStyle.danger
                else:
                    child.style = discord.ButtonStyle.secondary

            verdict = (
                f"✅ {interaction.user.mention} got it — **{correct:+d}** on block."
                if won else
                (
                    f"❌ {interaction.user.mention} guessed **{choice_value:+d}**. "
                    f"Correct answer was **{correct:+d}** on block."
                )
            )
            embed = discord.Embed(
                title=f"What's That Move? · {self.move.character} {self.move.notation}",
                description=verdict,
                color=discord.Color.from_rgb(*_safety_color(correct)),
            )
            embed.set_image(url="attachment://whats-that-move.png")
            await interaction.response.edit_message(
                embed=embed,
                attachments=[discord.File(buf, filename="whats-that-move.png")],
                view=self,
            )
        return callback

    async def on_timeout(self) -> None:
        # Disable buttons quietly when the quiz expires unanswered.
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True


class WhatsThatMove(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="whats-that-move",
        description="Frame-data quiz — guess the frames on block.",
    )
    async def whats_that_move(self, interaction: discord.Interaction) -> None:
        moves = frame_data.all_moves()
        if not moves:
            await interaction.response.send_message(
                "Frame-data starter set is empty — extend `frame_data.py`.",
                ephemeral=True, delete_after=10,
            )
            return
        move = random.choice(moves)
        choices = _generate_choices(move.frames_on_block)

        await interaction.response.defer()
        try:
            buf = await tournament_render.render_whats_that_move_card(
                character=move.character,
                notation=move.notation,
                move_name=move.name,
                revealed_frames=None,
            )
        except Exception:
            log.exception("[whats-that-move] render failed")
            await interaction.followup.send(
                "Couldn't render the quiz card — try again.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"What's That Move? · {move.character}",
            description=(
                f"Notation: `{move.notation}`\n"
                f"Move: **{move.name}**\n\n"
                "First click locks in the answer — first guess wins."
            ),
            color=discord.Color.from_rgb(200, 30, 40),
        )
        embed.set_image(url="attachment://whats-that-move.png")
        view = WhatsThatMoveView(move, choices)
        await interaction.followup.send(
            embed=embed,
            file=discord.File(buf, filename="whats-that-move.png"),
            view=view,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception,
    ) -> None:
        await handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WhatsThatMove(bot))
