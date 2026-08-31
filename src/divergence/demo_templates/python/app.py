"""Small deterministic simulation implementing the public command trace protocol."""

import json
import os
import sys
from pathlib import Path

from door import Door
from PIL import Image, ImageDraw


def render(state, path, renderer):
    width, height = 960, 540
    image = Image.new("RGB", (width, height), "#0e1520")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((38, 92, 922, 470), 16, fill="#182334", outline="#304255", width=2)
    for x in range(58, 920, 32):
        draw.line((x, 110, x, 450), fill="#1e2c3d")
    for y in range(110, 450, 32):
        draw.line((58, y, 902, y), fill="#1e2c3d")
    draw.text((40, 32), "SECTOR 07  /  DOOR INTERLOCK", fill="#c5d4e7", font_size=23)
    draw.text((40, 66), "DETERMINISTIC TEST ENVIRONMENT", fill="#778ca7", font_size=12)
    draw.rectangle((480, 94, 500, 215), fill="#455670")
    draw.rectangle((480, 335, 500, 467), fill="#455670")
    opened = state["door"]["open"]
    if not opened:
        draw.rectangle((480, 215, 500, 335), fill="#f0b66a")
    else:
        draw.line((480, 210, 500, 210), fill="#70dfc0", width=5)
        draw.line((480, 340, 500, 340), fill="#70dfc0", width=5)
    draw.rounded_rectangle((270, 145, 313, 184), 7, fill="#70dfc0" if opened else "#465671")
    draw.text((251, 122), "SWITCH", fill="#92a8c4", font_size=12)
    draw.ellipse((735, 250, 785, 300), outline="#54778a", width=2)
    draw.text((732, 315), "EXIT", fill="#92a8c4", font_size=12)
    x = state["player"]["x"]
    draw.ellipse((x - 18, 257, x + 18, 293), fill="#8f9bff", outline="#c4caff", width=3)
    draw.line((x, 275, x + 27, 275), fill="#c4caff", width=3)
    draw.text(
        (40, 496),
        "SCENARIO CLOCK     %05.2f s" % (state["clock_ms"] / 1000),
        fill="#9aadc8",
        font_size=16,
    )
    draw.text(
        (650, 496),
        "DOOR: " + ("OPEN" if opened else "CLOSED"),
        fill="#70dfc0" if opened else "#f0b66a",
        font_size=16,
    )
    if renderer == "pygame":
        import pygame

        surface = pygame.image.frombytes(image.tobytes(), image.size, "RGB")
        pygame.image.save(surface, str(path))
    else:
        image.save(path)


def main():
    config = json.loads(Path(os.environ["DIVERGENCE_SCENARIO"]).read_text())
    output = Path(os.environ["DIVERGENCE_OUTPUT"])
    (output / "frames").mkdir()
    renderer = "pygame" if "--pygame" in sys.argv else "pillow"
    if renderer == "pygame":
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        import pygame

        pygame.init()
        pygame.display.set_mode((960, 540))
    door = Door()
    x = 140.0
    samples = []
    for t in range(0, config["duration_ms"] + 1, config["sample_ms"]):
        events = []
        if t == 3000:
            door.activate()
            events.append("DoorOpened")
        if t >= 3500:
            target = min(760.0, x + config["sample_ms"] * 0.11)
            x = min(455.0, target) if door.collider_enabled else target
        state = {
            "door": {"open": door.open, "collider_enabled": door.collider_enabled},
            "player": {"x": round(x, 4), "reached_exit": x >= 750},
            "clock_ms": t,
        }
        frame = f"frames/{len(samples):05d}.png"
        render(state, output / frame, renderer)
        samples.append(
            {
                "t_ms": t,
                "state": state,
                "events": events,
                "logs": [],
                "metrics": {"simulation_step_ms": 1.0},
                "frame": frame,
            }
        )
    (output / "trace.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "seed": config["seed"],
                "samples": samples,
                "metadata": {
                    "adapter": renderer,
                    "metric_note": "simulation_step_ms is a synthetic fixture metric, not measured performance",
                },
            }
        )
    )
    if renderer == "pygame":
        pygame.quit()


if __name__ == "__main__":
    main()
