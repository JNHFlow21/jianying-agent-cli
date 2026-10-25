# Demo footage credits

The README demo (`docs/demo.gif`, `docs/demo.mp4`) is a 12-second film, *Nightfall*, rendered by 剪映's native engine through `jy draft build` + `jy render`,
from five time-lapses published on Wikimedia Commons. All five are works of U.S. National Park
Service employees and are in the **public domain** in the United States (`{{PD-USGov-NPS}}`).
The raw footage is not stored in this repository; `docs/render_demo.py` downloads it from the
URLs below. Edits applied: trim, reverse (clip 1) and 3.6× speed with jy's local engine, then 剪映
materials 叠化 (transition), 暗灰电影感 (filter, 60 %), 暗角 (video effect) and 渐显入场 (text animation)
in the native render. `python3 docs/render_demo.py --engine local` rebuilds an ffmpeg-only variant.

| # | Title | Author | Source | License |
|---|-------|--------|--------|---------|
| 1 | Sunrise Timelapse (played in reverse as a sunset) | NPS / N. Lewis, [Shenandoah National Park](https://www.flickr.com/people/snpphotos/) | [Commons](https://commons.wikimedia.org/wiki/File:Sunrise_Timelapse_(30174294051).webm) · [file](https://upload.wikimedia.org/wikipedia/commons/f/f1/Sunrise_Timelapse_%2830174294051%29.webm) | Public domain (PD-USGov-NPS); also tagged CC BY 2.0 on Flickr |
| 2 | Sunset Near Hidden Valley timelapse | NPS / Jesmira Bonoan, [Joshua Tree National Park](https://www.flickr.com/people/joshuatreenp/) | [Commons](https://commons.wikimedia.org/wiki/File:Sunset_Near_Hidden_Valley_timelapse_(40734114682).webm) · [file](https://upload.wikimedia.org/wikipedia/commons/e/e9/Sunset_Near_Hidden_Valley_timelapse_%2840734114682%29.webm) | Public domain (PD-USGov-NPS) |
| 3 | Dusk to night at Joshua Tree | NPS / Hannah Schwalbe, Joshua Tree National Park | [Commons](https://commons.wikimedia.org/wiki/File:Dusk_to_night_at_Joshua_Tree_(30497809811).webm) · [file](https://upload.wikimedia.org/wikipedia/commons/3/32/Dusk_to_night_at_Joshua_Tree_%2830497809811%29.webm) | Public domain (PD-USGov-NPS) |
| 4 | Stars and Clouds Over Headstone Rock | NPS / Hannah Schwalbe, Joshua Tree National Park | [Commons](https://commons.wikimedia.org/wiki/File:Stars_and_Clouds_Over_Headstone_Rock_(30230087163).webm) · [file](https://upload.wikimedia.org/wikipedia/commons/1/12/Stars_and_Clouds_Over_Headstone_Rock_%2830230087163%29.webm) | Public domain (PD-USGov-NPS) |
| 5 | Night Sky Timelapse (NPS_8667) | NPS / Brad Sutton, Joshua Tree National Park | [Commons](https://commons.wikimedia.org/wiki/File:Night_Sky_Timelapse_(30549248476).webm) · [file](https://upload.wikimedia.org/wikipedia/commons/e/e6/Night_Sky_Timelapse_%2830549248476%29.webm) | Public domain (PD-USGov-NPS) |

Credit is given as a courtesy; NPS does not endorse this project.
