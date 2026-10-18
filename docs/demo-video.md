# Demo video: shot list and narration (about 2 minutes)

Record on the laptop (QuickTime → New Screen Recording, with a phone filming the room, or the laptop camera).
Keep the caregiver page `http://qcventuno001.local:8000` visible in one window and the room in the other.
Before recording: camera shutter open and aimed at the floor (`/aim`), StopWatch on and showing "All quiet at home".

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:00–0:15 | The board with camera, Pixels and StopWatch on the table | "Falls are a leading cause of injury for older adults, and the person usually can't press a button. Guardian watches the room with a camera and a microphone, and nothing leaves the house." |
| 2 | 0:15–0:30 | Caregiver page: devices all green, "Vision: vlm" | "Everything runs on this Arduino VENTUNO Q. A cheap CPU check listens for thuds and watches for motion, then wakes InternVL3 on the Qualcomm NPU through ExecuTorch, about one second per check." |
| 3 | 0:30–0:50 | Teammate standing; drop a bag or book. Pixels sweep amber, then back to green. Watch shows "False alarm" | "A loud noise wakes the NPU, but it sees someone standing, so it's a false alarm and nobody is bothered." |
| 4 | 0:50–1:20 | Teammate lies down on the mat. Pixels: amber countdown bar. Page: "Asking them to get up" | "Now a real fall. The NPU confirms a person on the floor in about a second, Guardian asks them out loud whether they're OK, and the countdown runs on the microcontroller." |
| 5 | 1:20–1:40 | Countdown ends: Pixels flash red, the StopWatch buzzes and shows the model's description | "No response, so the family's watch gets the alert with what the camera saw." |
| 6 | 1:40–1:50 | Tap "I'm on my way" on the watch; everything returns to green | "One tap tells them help is coming." |
| 7 | 1:50–2:05 | Lie down again, then stand up during the countdown; it cancels | "If they get up, the NPU's recheck cancels it on its own: no false alarm reaches the family." |
| 8 | 2:05–2:15 | README measurements table | "Measured on the board: about one second per NPU check, 2.7 s from a thud to the check-in, and it all restarts by itself after a power cut." |

Tips
- Use the dashboard's **Thud (sound)** rehearsal button if a dropped object isn't loud enough on camera.
- The speaker isn't connected, so say the check-in line yourself in shot 4 ("Are you okay? … please get up").
- Keep shot 3 (false alarm) in: judges value that it doesn't cry wolf.
