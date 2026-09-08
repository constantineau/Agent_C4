"""Sensor health — would we have caught the kicked compass on Jul 18?

The crew reported that someone kicked the GPS/compass during the race and primary navigation
was lost. The fixture below is that event, straight out of `telemetry_raw`: per-minute
headingTrue (the 24xd — the ONLY own-ship heading publisher on this bus), COG and SOG from the
Orca, and the 24xd's own roll/pitch, from 20:00Z Jul 18 to 02:00Z Jul 19.

Three regimes, and each needs a different check to catch it:

  20:00–22:57Z  healthy. heading within a few degrees of COG; roll tracks the autopilot AHRS.
  22:58–23:59Z  violent. roll steps to 133° and then sits at ±175° (inverted), pitch −30..−49°,
                while the autopilot still reads 35°/−3°. A RANGE gate catches this on the first
                sample.
  00:00Z+       re-seated, and this is the dangerous one: every value looks perfectly ordinary
                — small roll, small pitch, a plausible heading number — and the compass is
                wrong by a quarter turn (−90°, held with 2–7° spread) for the rest of the trip.
                Only the CROSS-CHECK against GPS course catches it.

The battery was not involved: the archiver and the AIS receiver died at 20:40:30Z at the
voltage nadir, the kick came 2 h 18 m later, and by then the bank was recovering.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_sensor_health.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "vps", "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("DATA_SOURCE", "onboard")
os.environ.setdefault("ONBOARD_LIVE_WS", "false")

from app import sensor_health as sh   # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1784404800          # 2026-07-18T20:00:00Z
# (minutes after T0, headingTrue_24xd deg, COG_orca deg, SOG kn, roll_24xd deg, pitch_24xd deg)
KICK = [
    (0, 56.8, 64.1, 6.2, 20.8, 10.2), (1, 53.5, 54.3, 6.4, 29.7, 9.1), (2, 46.0, 42.5, 6.47, 24.4, 11.7), (3, 46.1, 43.7, 6.2, 23.4, 11.6),
    (4, 48.0, 49.2, 5.89, 30.4, 11.4), (5, 49.1, 53.3, 6.12, 22.5, 11.8), (6, 43.5, 46.1, 6.32, 20.9, 11.1), (7, 45.3, 48.8, 6.61, 24.0, 10.3),
    (8, 37.2, 40.3, 5.99, 29.1, 12.1), (9, 38.0, 39.7, 6.05, 25.6, 12.2), (10, 39.5, 42.5, 6.03, 18.0, 12.5), (11, 38.1, 38.0, 6.3, 27.5, 12.6),
    (12, 38.9, 39.6, 6.28, 27.8, 11.1), (13, 37.4, 35.8, 6.32, 16.1, 13.1), (14, 39.2, 41.9, 5.95, 22.9, 11.7), (15, 43.8, 53.0, 6.18, 17.1, 12.1),
    (16, 44.4, 44.8, 6.51, 21.2, 11.2), (17, 40.2, 36.7, 6.73, 15.4, 13.8), (18, 43.4, 45.2, 6.3, 16.2, 12.3), (19, 43.7, 39.4, 5.48, 20.9, 9.1),
    (20, 44.6, 53.5, 5.87, 21.7, 12.1), (21, 37.9, 44.4, 6.43, 18.8, 16.0), (22, 40.7, 42.3, 6.12, 22.8, 11.1), (23, 36.4, 36.6, 5.99, 25.8, 12.4),
    (24, 34.8, 39.7, 6.1, 22.5, 13.6), (25, 36.8, 45.4, 5.66, 28.9, 9.8), (26, 35.1, 36.2, 5.91, 28.1, 11.1), (27, 40.4, 44.0, 6.16, 25.3, 11.8),
    (28, 34.2, 24.5, 6.63, 20.2, 9.6), (29, 41.8, 50.9, 5.85, 22.4, 14.5), (30, 41.0, 47.0, 6.38, 22.3, 11.3), (31, 37.7, 37.6, 5.73, 31.8, 12.9),
    (32, 43.5, 47.2, 6.16, 17.2, 12.4), (33, 41.3, 53.4, 5.44, 26.6, 17.6), (34, 39.9, 41.2, 6.2, 24.9, 11.7), (35, 38.8, 38.1, 6.43, 20.3, 9.5),
    (36, 40.4, 49.6, 6.65, 20.0, 13.3), (37, 27.3, 27.5, 5.75, 28.0, 11.4), (38, 42.3, 46.3, 6.18, 18.1, 10.3), (39, 39.1, 40.3, 6.28, 18.2, 12.3),
    (40, 33.4, 35.1, 6.26, 29.3, 13.8), (41, 34.3, 24.8, 5.66, 23.0, 17.8), (42, 32.8, 49.3, 5.95, 20.5, 13.2), (43, 29.0, 31.4, 5.79, 31.7, 12.8),
    (44, 25.0, 20.9, 5.7, 30.7, 8.9), (45, 26.9, 22.9, 5.79, 18.9, 14.6), (46, 33.1, 33.5, 5.38, 29.4, 9.7), (47, 29.8, 33.9, 6.05, 27.7, 9.8),
    (48, 34.4, 30.6, 5.4, 23.8, 8.7), (49, 26.4, 23.3, 5.75, 28.6, 9.2), (50, 28.6, 32.7, 5.02, 27.1, 13.2), (51, 29.8, 24.1, 4.9, 25.5, 14.7),
    (52, 28.4, 42.0, 5.48, 24.7, 14.7), (53, 23.9, 30.0, 4.33, 26.5, 13.3), (54, 30.3, 27.0, 6.12, 20.2, 10.1), (55, 29.5, 34.2, 5.37, 21.9, 17.0),
    (56, 25.3, 19.1, 5.62, 17.4, 15.3), (57, 30.6, 36.2, 5.54, 29.2, 13.2), (58, 29.5, 40.3, 5.25, 24.3, 18.1), (59, 33.1, 42.3, 5.46, 31.6, 17.5),
    (60, 29.0, 27.4, 5.81, 34.8, 13.2), (61, 30.9, 42.7, 6.16, 30.3, 16.2), (62, 32.6, 45.7, 4.92, 39.1, 2.9), (63, 24.5, 18.9, 5.05, 30.7, 15.4),
    (64, 24.5, 10.6, 6.26, 24.3, 13.7), (65, 20.5, 24.3, 4.65, 22.9, 15.7), (66, 30.2, 28.5, 5.85, 29.1, 12.1), (67, 28.2, 26.4, 5.83, 22.3, 7.9),
    (68, 26.8, 32.1, 4.9, 22.6, 11.4), (69, 30.5, 24.0, 5.38, 27.3, 9.2), (70, 24.4, 21.9, 5.87, 20.6, 15.5), (71, 24.7, 21.7, 5.17, 18.2, 11.5),
    (72, 29.6, 19.7, 5.19, 30.4, 13.3), (73, 29.4, 35.5, 5.38, 34.7, 11.6), (74, 25.4, 21.7, 5.64, 29.2, 13.3), (75, 23.0, 14.3, 5.37, 18.1, 11.4),
    (76, 25.3, 30.6, 5.37, 21.5, 12.2), (77, 25.8, 33.8, 4.74, 36.1, 6.6), (78, 22.6, 14.0, 5.48, 22.1, 12.2), (79, 22.1, 16.1, 5.83, 14.5, 16.0),
    (80, 27.2, 19.1, 6.06, 21.9, 9.3), (81, 26.8, 23.6, 5.62, 25.6, 11.8), (82, 23.9, 13.3, 5.77, 25.5, 14.0), (83, 26.8, 23.1, 6.14, 36.9, 11.2),
    (84, 22.4, 11.5, 5.75, 22.6, 16.2), (85, 23.9, 12.2, 5.83, 23.8, 12.0), (86, 23.2, 14.9, 5.91, 26.2, 11.5), (87, 27.0, 31.4, 5.17, 29.8, 8.5),
    (88, 17.6, 0.2, 4.94, 13.7, 9.4), (89, 23.1, 6.9, 5.54, 27.5, 10.1), (90, 28.6, 39.8, 5.48, 28.5, 20.0), (91, 27.8, 17.1, 4.96, 18.1, 6.7),
    (92, 25.3, 17.0, 5.95, 26.5, 12.4), (93, 25.9, 32.2, 5.73, 22.3, 11.9), (94, 17.5, 2.8, 6.38, 14.1, 12.7), (95, 22.3, 11.1, 5.44, 20.0, 9.4),
    (96, 28.2, 30.0, 5.95, 25.2, 15.4), (97, 24.1, 27.7, 5.48, 27.5, 14.3), (98, 22.3, 12.2, 6.06, 29.0, 8.3), (99, 19.0, 359.7, 6.51, 22.2, 9.8),
    (100, 17.9, 4.3, 5.42, 22.8, 9.4), (101, 20.8, 5.7, 5.05, 24.4, 6.6), (102, 26.7, 23.2, 5.44, 28.8, 10.0), (103, 22.4, 9.8, 6.03, 31.3, 7.6),
    (104, 21.5, 10.3, 5.81, 19.9, 13.1), (105, 25.3, 21.4, 6.63, 29.7, 16.9), (106, 16.7, 355.1, 5.81, 19.5, 6.6), (107, 22.7, 8.7, 5.79, 28.2, 8.6),
    (108, 24.2, 17.2, 5.83, 27.8, 9.5), (109, 29.4, 16.4, 5.23, 27.9, 6.4), (110, 31.1, 14.5, 4.94, 22.2, 7.5), (111, 28.3, 20.6, 4.41, 27.2, 1.4),
    (112, 30.5, 14.4, 6.59, 26.3, 10.7), (113, 34.2, 32.5, 7.31, 23.3, 16.2), (114, 22.1, 356.3, 5.87, 26.3, 6.3), (115, 26.4, 24.2, 4.7, 30.2, 3.9),
    (116, 26.0, 11.3, 5.29, 26.4, 6.4), (117, 21.5, 357.5, 5.95, 17.6, 9.7), (118, 38.4, 23.2, 6.36, 28.2, 10.4), (119, 24.3, 2.1, 4.78, 25.5, 8.3),
    (120, 37.2, 30.8, 6.73, 35.6, 8.0), (121, 34.1, 16.6, 6.22, 23.4, 11.1), (122, 33.1, 19.1, 6.53, 26.7, 9.5), (123, 45.2, 42.4, 5.79, 23.5, 7.9),
    (124, 35.5, 27.2, 5.87, 26.7, 6.4), (125, 33.8, 11.8, 6.28, 24.8, 6.8), (126, 38.0, 13.0, 6.05, 26.5, 8.1), (127, 35.9, 10.4, 6.05, 23.3, 6.8),
    (128, 33.9, 12.0, 6.43, 19.6, 12.5), (129, 34.4, 22.2, 5.91, 27.3, 6.5), (130, 43.7, 30.0, 6.05, 35.9, 10.4), (131, 27.3, 5.8, 5.23, 27.9, 5.7),
    (132, 35.6, 19.8, 5.64, 21.8, 6.9), (133, 36.2, 9.8, 5.77, 30.2, 4.2), (134, 35.7, 20.8, 5.33, 27.8, 5.7), (135, 33.9, 10.1, 5.89, 21.2, 10.3),
    (136, 31.7, 20.2, 5.15, 24.5, 7.8), (137, 28.9, 11.0, 5.0, 25.8, 3.3), (138, 26.7, 15.1, 6.71, 26.8, 14.1), (139, 19.6, 2.7, 6.18, 18.6, 8.4),
    (140, 28.3, 16.7, 5.7, 19.1, 6.3), (141, 29.8, 11.4, 5.44, 15.2, 6.8), (142, 23.9, 359.2, 5.27, 16.5, 8.2), (143, 30.2, 13.9, 5.93, 26.7, 10.9),
    (144, 26.8, 13.6, 5.68, 29.2, 3.4), (145, 34.2, 25.9, 6.1, 28.2, 11.6), (146, 26.9, 0.4, 5.23, 25.0, 6.0), (147, 30.2, 20.6, 5.15, 34.6, 5.1),
    (148, 32.8, 25.7, 5.23, 32.0, 12.4), (149, 29.8, 18.2, 6.06, 26.3, 6.4), (150, 30.2, 21.5, 5.17, 30.7, 2.9), (151, 26.5, 12.5, 5.99, 24.4, 7.9),
    (152, 28.4, 16.7, 4.72, 18.9, 6.5), (153, 33.9, 5.5, 6.41, 21.5, 10.2), (154, 35.5, 21.8, 5.38, 26.9, 6.2), (155, 25.0, 6.3, 5.85, 15.6, 6.1),
    (156, 31.9, 21.3, 5.56, 32.2, 5.3), (157, 29.6, 15.6, 5.64, 19.5, 6.7), (158, 45.6, 36.9, 5.91, 34.2, 1.9), (159, 31.3, 6.8, 6.67, 31.5, 2.7),
    (160, 36.9, 14.7, 6.08, 22.4, 8.8), (161, 40.7, 44.4, 6.12, 36.3, 1.2), (162, 36.2, 35.5, 4.96, 25.7, 5.2), (163, 43.2, 35.8, 6.03, 31.1, 7.1),
    (164, 37.8, 14.0, 5.85, 21.9, 8.0), (165, 39.7, 7.6, 4.59, 31.2, 2.4), (166, 39.6, 31.0, 5.99, 32.8, 2.8), (167, 33.5, 18.6, 5.17, 23.4, 7.7),
    (168, 34.4, 17.7, 5.99, 28.0, 11.8), (169, 43.0, 20.1, 5.87, 24.5, 12.7), (170, 34.5, 24.3, 5.89, 36.1, -1.5), (171, 34.1, 4.7, 6.34, 26.8, 8.3),
    (172, 39.0, 16.0, 6.57, 25.7, 12.1), (173, 31.2, 20.9, 4.86, 17.4, 5.0), (174, 38.6, 19.6, 6.18, 27.5, 9.6), (175, 31.6, 22.4, 5.37, 27.6, 8.5),
    (176, 33.6, 23.9, 6.45, 35.7, 1.7), (177, 35.8, 22.8, 5.6, 36.4, 2.8), (178, 51.0, 19.4, 5.64, 133.1, -49.2), (179, 60.1, 9.7, 6.01, 149.6, -41.2),
    (180, 53.4, 18.7, 6.3, 160.2, -45.5), (181, 35.5, 1.7, 6.28, 155.7, -40.7), (182, 22.7, 31.0, 5.85, -172.7, -44.1), (183, 45.3, 359.7, 6.43, 164.3, -34.1),
    (184, 16.4, 34.5, 6.36, -167.3, -40.4), (185, 21.2, 28.6, 6.32, -174.8, -37.0), (186, 2.9, 14.3, 5.5, -174.3, -36.8), (187, 14.3, 14.2, 6.59, -175.5, -33.8),
    (188, 17.6, 18.2, 6.32, -178.6, -28.5), (189, 10.0, 7.0, 5.07, -177.6, -32.1), (190, 16.0, 3.6, 6.34, 179.4, -32.0), (191, 5.5, 25.5, 6.51, -171.8, -30.3),
    (192, 352.2, 6.1, 6.12, -174.5, -39.8), (193, 2.9, 12.3, 5.54, -176.1, -37.4), (194, 13.4, 27.4, 6.4, -159.5, -30.6), (195, 22.3, 29.3, 5.6, 176.5, -34.6),
    (196, 1.2, 357.6, 6.28, -179.5, -39.2), (197, 13.1, 24.8, 5.23, -170.2, -25.9), (198, 9.7, 356.6, 6.34, 176.1, -27.4), (199, 342.4, 15.9, 5.54, -174.7, -27.2),
    (200, 18.7, 16.3, 5.64, -177.5, -29.8), (201, 73.0, 18.1, 2.39, 38.1, 6.2), (202, 143.8, 349.9, 0.87, -0.5, -1.2), (203, 219.3, 159.1, 1.67, -3.6, 1.8),
    (204, 237.4, 133.9, 3.95, -6.6, 0.3), (205, 15.2, 220.4, 2.14, 11.1, 6.2), (206, 356.7, 230.3, 4.72, 2.6, 9.1), (207, 24.0, 220.5, 1.59, 6.9, 7.8),
    (208, 20.8, 239.5, 2.97, 16.9, 15.8), (209, 29.3, 214.2, 2.18, 8.1, 5.4), (210, 28.8, 194.6, 1.65, 4.3, 4.8), (211, 13.5, 168.1, 4.22, 20.1, 18.2),
    (212, 344.6, 208.9, 3.42, 6.3, 17.7), (213, 233.5, 146.7, 3.15, -4.2, -4.5), (214, 205.6, 77.0, 3.58, 4.1, 4.4), (215, 188.0, 86.9, 1.56, 7.5, -1.1),
    (216, 201.1, 84.1, 2.59, 2.6, -1.3), (217, 194.2, 51.9, 2.97, 6.5, 2.2), (218, 215.8, 116.2, 1.98, 2.0, -0.4), (219, 147.1, 21.6, 5.54, -0.2, -7.8),
    (220, 140.0, 19.7, 3.48, 3.9, 3.7), (221, 142.7, 31.9, 3.19, 14.0, -3.8), (222, 142.0, 9.1, 5.56, -6.5, -18.5), (223, 140.8, 30.5, 6.14, -11.4, -20.2),
    (224, 131.8, 21.6, 6.3, -4.0, -14.0), (225, 58.2, 33.0, 6.71, 22.4, -16.5), (226, 302.8, 14.7, 5.85, 10.3, 30.6), (227, 18.2, 35.2, 4.8, 26.6, 27.8),
    (228, 1.7, 358.2, 6.8, 19.9, 26.6), (229, 20.1, 11.7, 7.17, 27.6, 21.2), (230, 27.6, 24.1, 5.52, 16.5, 16.0), (231, 30.4, 18.2, 4.37, 17.6, 17.9),
    (232, 30.4, 24.7, 4.8, 12.3, 17.2), (233, 45.0, 42.4, 4.68, 43.6, 18.8), (234, 24.6, 6.0, 4.28, 1.4, 22.4), (235, 125.8, 32.7, 4.74, -0.9, -12.0),
    (236, 272.5, 30.5, 5.07, -6.3, 26.5), (237, 283.2, 10.6, 5.0, 7.7, 29.3), (238, 292.9, 42.0, 3.98, 2.2, 13.7), (239, 288.3, 14.1, 4.92, 4.0, 13.2),
    (240, 288.9, 32.1, 5.54, 2.6, 26.5), (241, 289.0, 18.2, 5.5, 2.6, 20.8), (242, 286.0, 23.9, 5.33, 4.4, 4.5), (243, 289.0, 23.6, 5.85, -5.9, 23.4),
    (244, 285.7, 32.1, 5.37, 1.2, 26.2), (245, 283.0, 27.9, 5.81, -3.6, 27.4), (246, 286.6, 23.1, 5.7, 1.5, 25.9), (247, 275.9, 19.1, 5.35, -4.8, 25.1),
    (248, 294.1, 41.4, 6.32, 5.5, 35.5), (249, 283.2, 33.1, 7.1, 3.3, 35.7), (250, 283.1, 20.9, 5.79, 7.4, 30.0), (251, 286.7, 21.8, 5.79, 1.2, 18.3),
    (252, 285.7, 24.6, 6.24, 4.8, 27.3), (253, 297.1, 45.3, 5.48, 7.6, 39.4), (254, 278.4, 13.4, 6.59, 3.7, 29.2), (255, 284.2, 31.7, 6.41, -2.0, 34.9),
    (256, 289.3, 25.2, 5.46, -3.2, 18.4), (257, 279.2, 19.0, 5.79, -0.2, 29.1), (258, 288.8, 35.3, 5.38, 0.1, 27.3), (259, 292.4, 30.5, 6.8, 5.1, 27.5),
    (260, 288.4, 32.8, 6.55, -1.4, 30.0), (261, 289.6, 22.7, 6.1, 2.1, 18.3), (262, 289.2, 24.7, 6.82, 5.2, 27.9), (263, 292.3, 31.7, 6.14, 5.1, 27.1),
    (264, 283.2, 12.3, 5.27, 2.2, 23.6), (265, 290.1, 27.1, 6.06, -3.7, 23.8), (266, 291.1, 34.0, 5.6, -7.6, 19.6), (267, 290.4, 34.3, 6.36, 1.7, 18.5),
    (268, 280.4, 16.9, 6.06, 1.4, 34.2), (269, 320.8, 21.8, 1.48, 2.6, 9.9), (270, 357.5, 112.5, 0.89, 1.1, 5.8), (271, 350.9, 136.1, 1.42, -0.7, 10.3),
    (272, 343.4, 155.0, 1.17, -0.3, 9.7), (273, 6.8, 139.9, 1.01, 5.7, 5.2), (274, 2.3, 130.9, 1.13, -1.7, 7.7), (275, 338.2, 27.7, 1.15, -0.7, -6.8),
    (276, 9.2, 100.9, 1.5, 1.9, 11.8), (277, 326.8, 83.0, 1.28, 1.9, 1.4), (278, 355.3, 123.5, 1.9, 0.1, 3.3), (279, 329.8, 70.5, 0.86, 2.5, -0.3),
    (280, 40.5, 135.8, 5.89, 2.5, -2.7), (281, 65.9, 164.7, 6.47, 2.6, 3.4), (282, 52.5, 140.2, 6.88, -3.7, -4.9), (283, 59.3, 149.7, 5.68, 1.3, 7.1),
    (284, 45.8, 129.0, 5.68, -5.0, -7.0), (285, 81.0, 170.2, 8.65, 7.7, -11.3), (286, 57.0, 147.5, 6.45, -3.8, -4.8), (287, 50.0, 144.5, 9.58, 1.8, -2.3),
    (288, 77.9, 165.5, 7.87, -4.6, -2.4), (289, 87.5, 180.7, 5.64, 1.9, 5.7), (290, 86.1, 172.0, 6.16, -1.5, -2.7), (291, 123.7, 226.7, 5.35, 4.1, -1.7),
    (292, 113.5, 206.4, 5.99, 2.7, -3.5), (293, 71.5, 162.8, 10.22, -1.9, 24.6), (294, 104.3, 187.2, 8.16, 9.8, -19.0), (295, 130.1, 234.2, 3.97, 2.7, 12.4),
    (296, 95.4, 189.1, 7.58, 2.3, 4.0), (297, 101.3, 190.6, 7.64, 5.0, -4.3), (298, 82.0, 168.5, 9.74, -5.2, 1.5), (299, 109.0, 194.9, 6.18, -1.9, -0.2),
    (300, 94.3, 192.2, 6.75, 5.0, 13.4), (301, 89.9, 180.8, 8.18, 6.3, -0.6), (302, 90.5, 182.8, 9.76, -2.9, 11.1), (303, 136.8, 223.1, 10.46, -2.1, -5.8),
    (304, 113.9, 208.9, 7.79, 4.0, 7.5), (305, 115.2, 203.5, 8.11, -2.3, 2.3), (306, 95.5, 191.1, 7.33, 2.1, 14.7), (307, 94.2, 183.7, 5.35, 0.4, 14.3),
    (308, 105.4, 190.4, 11.2, 0.0, 8.2), (309, 101.5, 195.5, 9.0, 5.0, 13.3), (310, 107.0, 193.1, 7.89, -1.7, 6.6), (311, 101.1, 188.3, 8.2, -1.7, -0.6),
    (312, 128.2, 215.0, 7.46, 0.0, 0.1), (313, 125.6, 215.0, 7.99, 5.0, 2.7), (314, 115.5, 212.4, 8.55, 4.2, 4.4), (315, 111.8, 203.3, 7.35, 3.9, 6.0),
    (316, 101.8, 196.6, 7.85, -0.4, 15.8), (317, 95.3, 176.3, 6.34, 3.0, -5.5), (318, 100.9, 186.1, 9.39, 2.3, 2.6), (319, 109.9, 195.6, 7.46, -1.1, -4.8),
    (320, 89.2, 175.2, 4.57, 2.7, 4.3), (321, 94.0, 185.2, 7.19, 5.9, 18.8), (322, 109.8, 195.3, 7.68, -0.3, 6.2), (323, 109.6, 195.3, 7.41, 0.8, -4.1),
    (324, 107.0, 197.1, 6.9, 5.6, -5.5), (325, 115.2, 195.5, 6.75, 2.1, -4.8), (326, 98.3, 188.9, 9.84, 1.2, 10.7), (327, 95.6, 180.8, 6.88, 6.3, -4.2),
    (328, 94.6, 187.9, 6.55, 6.0, 2.8), (329, 100.7, 190.0, 9.68, -5.5, 0.7), (330, 99.3, 193.0, 7.33, 7.0, -7.5), (331, 108.2, 199.1, 10.32, -0.5, 1.4),
    (332, 91.2, 198.6, 3.6, 2.0, 10.2), (333, 109.8, 193.9, 5.66, 10.0, -2.0), (334, 90.5, 179.5, 10.11, 0.3, 11.3), (335, 102.3, 192.7, 7.56, 4.7, -4.0),
    (336, 83.5, 184.4, 5.68, -1.7, 14.6), (337, 100.6, 192.1, 5.99, 4.1, 7.6), (338, 112.7, 199.7, 8.79, -7.9, 0.9), (339, 105.5, 196.2, 8.11, 1.2, -12.3),
    (340, 97.1, 185.6, 7.29, -0.2, 7.0), (341, 85.7, 182.1, 5.58, 3.8, 16.4), (342, 101.1, 177.5, 7.5, 1.5, -4.2), (343, 97.5, 185.3, 5.87, 1.6, -1.5),
    (344, 101.6, 191.4, 10.05, 6.8, 8.3), (345, 132.3, 216.8, 9.64, 2.7, -6.3), (346, 119.8, 204.9, 9.76, -0.0, -5.5), (347, 99.5, 185.3, 7.83, 8.5, 10.2),
    (348, 97.3, 183.7, 7.91, -0.4, 2.8), (349, 92.5, 184.3, 11.33, 3.9, 12.4), (350, 97.6, 183.4, 7.56, 1.7, 11.8), (351, 104.3, 191.9, 9.23, 1.7, 11.6),
    (352, 115.1, 206.4, 6.16, 2.3, 2.3), (353, 100.9, 204.5, 5.09, 0.8, 19.1), (354, 83.7, 173.7, 6.84, 6.1, 1.3), (355, 119.8, 213.5, 7.43, 1.1, 14.0),
    (356, 106.8, 200.5, 7.74, -0.9, 6.3), (357, 102.1, 185.8, 11.47, 0.9, -6.7), (358, 103.8, 195.2, 9.72, 1.1, 8.6), (359, 107.6, 192.4, 10.87, -3.5, 6.8),
    (360, 105.2, 193.9, 9.19, 3.8, 13.9),
]

KICK_MIN = 178          # 22:58Z — the first bad attitude sample
RESEATED_MIN = 201      # 23:21Z — 23 minutes later the crew put it back upright
# ...and the quarter-turn yaw error then persisted for the remaining ~7 hours, invisible.


def window(end_min, minutes=20):
    """[(epoch, hdg, cog, sog)] for the `minutes` before `end_min`, as the engine would read."""
    return [(T0 + m * 60, h, c, s) for (m, h, c, s, _r, _p) in KICK
            if end_min - minutes <= m <= end_min]


def attitude_at(m):
    for (mm, _h, _c, _s, r, p) in KICK:
        if mm == m:
            return sh.attitude_plausible(r, p)
    raise AssertionError(f"no sample at T0+{m}")


# --- the healthy race -------------------------------------------------------
print("healthy (20:00-20:40Z, before the drift):")
for m in (10, 25, 35):
    a = attitude_at(m)
    check(f"  T0+{m}: attitude passes the range gate ({a['roll_deg']:+.1f}° roll)", a["ok"])
h = sh.heading_bias(window(35))
check(f"heading agrees with GPS course (bias {h['bias_deg']:+.1f}°, spread {h['spread_deg']}°)",
      h["status"] == "ok")

# --- the drift, BEFORE anyone kicked anything -------------------------------
# Measured hourly: -0.1..-5.7 deg through the healthy race, then +7.0 at 21:00Z and +16.3 at
# 22:00Z. Something was already wrong an hour before the visible event — a compass working
# loose, or a genuinely big leeway/current leg. Either way the crew was never told.
print("the drift before the kick:")
drift = sh.heading_bias(window(KICK_MIN - 1))   # the engine's default 20-min window
check(f"the 22:00-22:56Z drift is surfaced, not swallowed (bias {drift['bias_deg']:+.1f}°, "
      f"{drift['status']})", drift["status"] == "warn")

# --- the kick ---------------------------------------------------------------
print("the kick (22:58Z):")
before, after = attitude_at(KICK_MIN - 1), attitude_at(KICK_MIN)
check(f"the sample before is fine ({before['roll_deg']:+.1f}° / {before['pitch_deg']:+.1f}°)",
      before["ok"])
check(f"the FIRST bad sample is caught ({after['roll_deg']:+.1f}° / "
      f"{after['pitch_deg']:+.1f}°)", not after["ok"] and after["status"] == "danger")
check("...and says why", "roll" in after["reason"])
flagged = [m for (m, _h, _c, _s, r, p) in KICK
           if KICK_MIN <= m < RESEATED_MIN and not sh.attitude_plausible(r, p)["ok"]]
check(f"every minute of the inverted period is flagged ({len(flagged)}/"
      f"{RESEATED_MIN - KICK_MIN})", len(flagged) == RESEATED_MIN - KICK_MIN)

# --- re-seated: everything looks fine and is 90 degrees wrong ---------------
print("re-seated (23:21Z+) — the quiet failure:")
late = [(m, r, p) for (m, _h, _c, _s, r, p) in KICK if m >= RESEATED_MIN + 10]
sane = [m for (m, r, p) in late if sh.attitude_plausible(r, p)["ok"]]
check(f"the range gate goes silent again ({len(sane)}/{len(late)} samples 'plausible') — "
      f"which is exactly the trap", len(sane) > 0.9 * len(late))
for m in (280, 320, 360):
    hb = sh.heading_bias(window(m))
    check(f"  T0+{m}: the cross-check still catches it (bias {hb['bias_deg']:+.0f}°, "
          f"spread {hb['spread_deg']}°)", hb["status"] == "danger")
hb = sh.heading_bias(window(320))
check("...and the wording blames the compass, not the boat",
      "compass" in hb["reason"] and "misaligned" in hb["reason"])
check("the bias is a quarter turn, and that is worth stating precisely",
      -100 <= hb["bias_deg"] <= -80)

# --- guards -----------------------------------------------------------------
print("guards:")
check("a tumbling sensor is 'unknown' to the bias check, not a false bias",
      sh.heading_bias([(T0 + i * 60, (i * 47) % 360, 10.0, 6.0) for i in range(20)])["status"]
      == "unknown")
check("at rest there is nothing to compare (COG is meaningless)",
      sh.heading_bias([(T0 + i * 60, 40.0, 200.0, 0.0) for i in range(20)])["status"]
      == "unknown")
check("too few samples -> unknown, not a verdict",
      sh.heading_bias([(T0, 40.0, 130.0, 6.0)])["status"] == "unknown")
check("empty input is safe", sh.heading_bias([])["available"] is False)
check("missing attitude is not a failure", sh.attitude_plausible(None, None)["ok"] is True)
check("the wrap is handled: 359 vs 1 is 2 degrees, not 358",
      abs(sh.heading_bias([(T0 + i * 60, 359.0, 1.0, 6.0)
                           for i in range(20)])["bias_deg"]) < 3)

print("\nthe event, as the checks see it:")
for m in (25, 150, 177, 178, 190, 205, 280, 360):
    a = attitude_at(m)
    hb = sh.heading_bias(window(m))
    print(f"  T0+{m:>3} min  roll {a['roll_deg']:>7.1f}  pitch {a['pitch_deg']:>6.1f}  "
          f"range={a['status']:<7} bias={str(hb.get('bias_deg')):>7}  cross={hb['status']}")


# ===========================================================================
# PROVENANCE — where is each number FROM, and is the policy actually in force?
# ===========================================================================
# Added 2026-09-08 with the iPad's instrument-health chip. Channels are assembled through the
# REAL `onboard_conditions._choose_preferred`, because `fell_back` is its output and a fixture
# that set the flag by hand would test the display and not the decision.
from app import onboard_conditions as oc   # noqa: E402

ORCA, REACTOR, G24XD, GND10, AIS = ("n2k-socketcan.15", "n2k-socketcan.1", "n2k-socketcan.3",
                                    "n2k-socketcan.0", "n2k-socketcan.43")


def channels(spec):
    """{channel: (unit, [(source, value, age_s), ...])} -> a `/conditions/full` channels dict."""
    out = {}
    for ch, (unit, rows) in spec.items():
        readings = [{"source": s, "value": v, "age_s": a} for (s, v, a) in rows]
        c = {"unit": unit, "readings": readings,
             "freshest_age_s": min(r["age_s"] for r in readings)}
        vals = [r["value"] for r in readings]
        if len(vals) > 1:
            c["spread"] = round(max(vals) - min(vals), 3)
            c["disagreement"] = c["spread"] > oc.DISAGREE.get(unit, 1e9)
        best, reason, fell_back = oc._choose_preferred(ch, readings)
        from shared import n2k_sources as ns
        c["preferred"] = {"source": best["source"], "value": best["value"],
                          "age_s": best["age_s"],
                          "device": (ns.resolve(best["source"],
                                                ns.devices_for("sr33")) or {}).get("model")}
        c["preferred_reason"] = reason
        if fell_back:
            c["fell_back"] = True
        out[ch] = c
    return out


print("\nprovenance — the healthy case:")
# `derived-data` carries a reading on purpose: `derived` is the only matcher in the policy that
# names no bus device, so it can only be shown to bind by something actually publishing under
# that label. Leave it out and the policy check correctly complains.
HEALTHY_SPEC = {
    "tws": ("kn", [(ORCA, 17.2, 1.0)]),
    "twd": ("°", [(ORCA, 264.4, 1.0), ("derived-data", 263.9, 2.0)]),
    "stw": ("kn", [("n2k-socketcan.4", 7.6, 1.0)]),
    "sog": ("kn", [(G24XD, 7.4, 1.0), (ORCA, 7.3, 1.0)]),
    "aws": ("kn", [(GND10, 18.2, 1.0), (ORCA, 17.9, 2.0)]),
}
HEALTHY = channels(HEALTHY_SPEC)
p = sh.assess_provenance(HEALTHY, ais_excluded={AIS})
check(f"clean bus reads ok ({p['reason'][:48]}…)", p["status"] == "ok" and not p["flags"])
check("every matcher in the committed policy binds to a device on this bus",
      p["checks"]["policy_binds"]["status"] == "ok"
      and not p["checks"]["policy_binds"]["unresolvable"])
check("the provenance table names the DEVICE, not the N2K address",
      p["channels"]["aws"]["device"] == "GND10" and p["channels"]["tws"]["device"] == "Orca Core")
check("...with its rank, so 'is this the sensor we chose?' is answerable",
      p["channels"]["aws"]["rank"] == 1 and p["channels"]["sog"]["rank"] == 1)
check("the AIS filter reports what it is excluding — positive evidence it bound",
      p["checks"]["own_ship"]["ais_excluded"] == [AIS]
      and p["checks"]["own_ship"]["status"] == "ok")

print("provenance — the rank-1 sensor goes silent mid-race:")
# The GND10 masthead stops: its last reading is 90 s old, past the 45 s failover window, so the
# Orca's computed apparent wind takes over. Measured on Jul 18: those two disagree by up to
# 5.9 kn, so which one is on screen is not a detail.
SILENT = channels(dict(HEALTHY_SPEC, aws=("kn", [(GND10, 18.2, 90.0), (ORCA, 12.3, 1.0)])))
p = sh.assess_provenance(SILENT, ais_excluded={AIS})
check(f"it is a WARN, not a note ({p['checks']['lead_source']['reason'][:60]}…)",
      p["status"] == "warn" and p["checks"]["lead_source"]["status"] == "warn")
ws = p["checks"]["lead_source"]["went_silent"]
check("the flag says which channel, which backup is in use, and how stale the lead is",
      len(ws) == 1 and ws[0]["channel"] == "aws" and ws[0]["expected"] == "gnd"
      and ws[0]["lead_age_s"] == 90.0)
check("...and the chip has a one-line reason it can show verbatim",
      "aws" in p["reason"] and "went silent" in p["reason"])

print("provenance — a ranked lead that has NEVER published (Jul 18, all race):")
# The policy ranks the Orca first for heel/pitch/rate-of-turn. The Orca published none of them
# during the race — its N2K attitude sharing is off — so heel ran on the 24xd for seven hours.
# True on every poll of every race until someone changes a setting on the boat, so it must NOT
# read as an alarm: a permanently yellow chip is a chip nobody looks at.
UNMET = channels(dict(HEALTHY_SPEC,
                      heel=("°", [(G24XD, 14.8, 1.0), (REACTOR, 15.1, 1.0)])))
p = sh.assess_provenance(UNMET, ais_excluded={AIS})
check("the status stays ok — this is configuration, not an event",
      p["status"] == "ok" and not p["flags"])
check("but it is NOT silent: the note names the channel and the device that owes it data",
      any("heel" in n and "orca" in n for n in p["notes"]))
check("the table still records that heel is not on its rank-1 source",
      p["channels"]["heel"]["fell_back"] is True
      and p["channels"]["heel"]["lead_publishes"] is False)
check("a channel whose lead DOES publish is distinguished from one whose lead never has",
      p["channels"]["tws"]["lead_publishes"] is True)

print("provenance — AIS traffic leading an own-ship channel (the P0 regression):")
# If `datasource_onboard`'s AIS exclusion is ever switched off or fails to identify the
# transceiver, another vessel's position leads own-ship position again — max 4,091 kn as raced.
# A range check would not catch it (5.6 kn median looks fine); the source identity does.
BAD = channels(dict(HEALTHY_SPEC, lat=("°", [(AIS, 41.2, 1.0)]),
                    lon=("°", [(AIS, -2.4, 1.0)])))
p = sh.assess_provenance(BAD, ais_excluded={AIS})
check(f"it is a DANGER ({p['checks']['own_ship']['reason'][:52]}…)",
      p["status"] == "danger" and p["checks"]["own_ship"]["status"] == "danger")
check("...naming the channels being contaminated",
      {x["channel"] for x in p["checks"]["own_ship"]["ais_leading"]} == {"lat", "lon"})

print("provenance — a policy that cannot bind (the defect shape, six times over):")
# `orca`/`24xd`/`reactor` matched NOTHING for most of this project's life, because `$source` is
# an N2K address. An unmatched matcher and a satisfied one look identical unless asserted.
import shared.source_policy as sp   # noqa: E402
_orig = sp.DEFAULT_PRIORITY
try:
    sp.DEFAULT_PRIORITY = dict(_orig, tws=["garmin-gwind", "orca"])
    p = sh.assess_provenance(HEALTHY, ais_excluded={AIS})
    check("a matcher naming no device on the bus is a warn, not silence",
          p["status"] == "warn" and "garmin-gwind" in p["checks"]["policy_binds"]["unresolvable"])
    check("...and says what it means: those channels are unranked in practice",
          "unranked in practice" in p["checks"]["policy_binds"]["reason"])
finally:
    sp.DEFAULT_PRIORITY = _orig

print("provenance — guards:")
check("no channels at all is 'unknown', never a false all-clear",
      sh.assess_provenance({})["status"] == "unknown")
check("...and says so", "no live channels" in sh.assess_provenance({})["reason"])
check("a computed channel is marked as arithmetic, not measurement",
      sh.assess_provenance(channels(dict(HEALTHY_SPEC,
                                         twd=("°", [("derived-data", 264.4, 1.0)]))))
        ["channels"]["twd"]["measured"] is False)
check("...and a measured one is not",
      sh.assess_provenance(HEALTHY)["channels"]["aws"]["measured"] is True)
check("an unmapped source degrades to its raw label rather than raising",
      sh.assess_provenance(channels(dict(HEALTHY_SPEC,
                                         depth=("m", [("bench-provider", 12.4, 1.0)]))))
        ["channels"]["depth"]["source"] == "bench-provider")


# ===========================================================================
# THE REFERENCE — what is this cross-check actually comparing against?
# ===========================================================================
# Added 2026-09-08. Nothing asserted `assess()`'s READ PATH before this: every test above hands
# `heading_bias` a fixture of pre-paired samples, so the module looked correct while the thing
# that fed it read COG through `src.series()` — one value per second, whichever source wrote
# last — and `source_priority` handed it the compass's own box as rank 1. Both facts were
# invisible to a test that never let the module choose its own reference.
from shared import n2k_sources as ns   # noqa: E402

DEVICES = ns.devices_for("sr33")


def rows(*specs):
    """(source, n) pairs -> a `series_by_source` result, one sample a second."""
    out = []
    for src, n in specs:
        out += [(src, T0 + i, 0.5) for i in range(n)]
    return out


print("\nthe reference — which device is the cross-check comparing against?")
src, indep = sh.choose_source(rows((G24XD, 60), (ORCA, 60)), "cog", DEVICES,
                              exclude={sh._device_key(G24XD, DEVICES)})
check("COG comes off the Orca, NOT the 24xd that publishes the heading — even though "
      "source_priority ranks the 24xd first", (src, indep) == (ORCA, True))
check("...and the policy really does rank it first, so this is a live trap, not a hypothetical",
      sp.matchers_for("cog")[0] == "24xd" and ns.matches(G24XD, "24xd", DEVICES))
src, indep = sh.choose_source(rows((G24XD, 60)), "cog", DEVICES,
                              exclude={sh._device_key(G24XD, DEVICES)})
check("with no independent publisher it degrades rather than going blind, and says so",
      (src, indep) == (G24XD, False))
src, _ = sh.choose_source(rows((AIS, 200), (ORCA, 60)), "cog", DEVICES,
                          exclude={sh._device_key(G24XD, DEVICES)})
check("the AIS transceiver is refused outright — source_priority lists b951 as a COG fallback, "
      "and another vessel's course is not own-ship", src == ORCA)
check("...and it is refused even when it is the ONLY publisher",
      sh.choose_source(rows((AIS, 200)), "cog", DEVICES)[0] is None)
check("no publishers at all is None, not a crash", sh.choose_source([], "cog", DEVICES)[0] is None)
# `sqrt(-2 ln R)` with every sample identical: R overshoots 1 by an ulp, ln goes positive, sqrt
# raises. A becalmed boat and every bench fixture produce exactly that, and it took `assess()`
# down with a ValueError rather than returning a verdict.
check("perfect agreement is 0° of spread, not a ValueError",
      sh.heading_bias([(T0 + i, 40.0, 130.0, 6.0) for i in range(20)])["spread_deg"] == 0.0)
check("the pick is deterministic — the same window cannot yield two verdicts",
      sh.choose_source(rows((ORCA, 60), (G24XD, 60)), "cog", DEVICES)[0]
      == sh.choose_source(rows((G24XD, 60), (ORCA, 60)), "cog", DEVICES)[0])
check("an unmapped label is its own device, so a bench is never told two sources are one",
      sh._device_key("bench-provider", DEVICES) != sh._device_key(ORCA, DEVICES))
check("...and two addresses sharing a modelSerialCode are still two devices "
      "(.5 and .11 both report 3432723336)",
      sh._device_key("n2k-socketcan.5", DEVICES) != sh._device_key("n2k-socketcan.11", DEVICES))


class FakeSource:
    """The read path `assess()` actually uses, with two COG publishers that disagree — which is
    the Jul 18 bus. `series_by_source` only; a source that cannot name its publishers has no
    business feeding a cross-check."""

    def __init__(self, bias_deg=-90.0, cog_split=25.0, n=120):
        self.bias, self.split, self.n = bias_deg, cog_split, n

    def latest_value(self, path):
        return {"navigation.attitude.roll": 0.20, "navigation.attitude.pitch": 0.04}.get(path)

    def series_by_source(self, path, minutes):
        rad = 0.017453292519943295
        out = []
        for i in range(self.n):
            t, cog = T0 + i * 5, 30.0 + (i % 7)          # a boat holding a course
            if path == "navigation.headingTrue":
                out.append((G24XD, t, ((cog + self.bias) % 360) * rad))
            elif path == "navigation.courseOverGroundTrue":
                out.append((ORCA, t, cog * rad))
                out.append((G24XD, t, ((cog + self.split) % 360) * rad))
            elif path == "navigation.speedOverGround":
                out += [(ORCA, t, 3.1), (G24XD, t, 3.1)]
        return out

    def ais_sources(self):
        return {AIS}


h = sh.assess(source=FakeSource(), conditions={"channels": {}})
check(f"end to end, the quarter turn is caught (bias {h['heading'].get('bias_deg')}°, "
      f"spread {h['heading'].get('spread_deg')}°)", h["heading"]["status"] == "danger")
check("...against the Orca, and the verdict says which device",
      h["heading"]["reference"]["course"] == "Orca Core"
      and "Orca Core" in h["heading"]["reason"])
check("...and claims independence only because it really got it",
      h["heading"]["reference"]["independent"] is True and not h["heading"]["note"])
# The regression that mattered: the SAME data read through `series()` mixes two publishers 25°
# apart into one series, and that manufactured spread is what a spread gate sees.
mixed = [(t, hh, cc, ss) for (t, hh, cc, ss) in
         [(T0 + i * 5, (30.0 + (i % 7) - 90.0) % 360,
           (30.0 + (i % 7) + (25.0 if i % 2 else 0.0)) % 360, 6.0) for i in range(120)]]
check("a two-publisher reference inflates the spread the gate judges by "
      f"({sh.heading_bias(mixed)['spread_deg']}° vs {h['heading']['spread_deg']}°)",
      sh.heading_bias(mixed)["spread_deg"] > 2 * h["heading"]["spread_deg"])

print("the reference — a bus with only one GPS:")


class SoloGPS(FakeSource):
    def series_by_source(self, path, minutes):
        return [r for r in FakeSource.series_by_source(self, path, minutes) if r[0] != ORCA]


h1 = sh.assess(source=SoloGPS(), conditions={"channels": {}})
check("the check still runs — a magnetometer and a position track are different physics",
      h1["heading"]["status"] == "danger")
check("...but it does not pretend to redundancy it does not have",
      h1["heading"]["reference"]["independent"] is False
      and "no COG publisher independent of" in (h1["heading"]["note"] or ""))
check("...and that rides as a NOTE, not a flag — it is true on every poll of every race",
      any("independent of" in n for n in h1["notes"])
      and not any("independent of" in f for f in h1["flags"]))

# --- the window is the detection latency, and that is what looked like a bug ----------------
# The first full-race replay read 35-of-36 `unknown` frames as "the check is silent on the data
# the boat sent". It was not: every one of those windows straddled the step at 23:56:10Z when
# the bias appeared. A sliding window cannot do anything else, and it clears itself.
print("the window straddling the fault — the false P0:")
STEP = 60          # samples before the bias appears


def straddle(after):
    """`after` samples of a −90° bias appended to STEP good ones, as the window slides over."""
    good = [(T0 + i * 5, 30.0 + (i % 7), 30.0 + (i % 7), 6.0) for i in range(STEP - after)]
    bad = [(T0 + (STEP - after + i) * 5, (30.0 + (i % 7) - 90.0) % 360, 30.0 + (i % 7), 6.0)
           for i in range(after)]
    return good + bad


check("a window that is half healthy and half misaligned reports unknown, not a false bias",
      sh.heading_bias(straddle(30))["status"] == "unknown")
check("...and says the bias may simply be new, rather than blaming the crew's steering",
      "only just appeared" in sh.heading_bias(straddle(30))["reason"])
check("one window later it is a clean danger — the quiet is self-clearing",
      sh.heading_bias(straddle(STEP))["status"] == "danger")
check("the window before the fault is a clean ok, so this is a transition and not a blind spot",
      sh.heading_bias(straddle(0))["status"] == "ok")
check("the default window is the detection latency, and 10 min is the measured choice",
      sh.HEADING_WINDOW_MIN == 10)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
