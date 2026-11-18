import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import match_automation as ma


def load(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return f.read()


class ScorecardMatchTests(unittest.TestCase):
    """Expected values come from the match's Full Scorecard page."""

    @classmethod
    def setUpClass(cls):
        cls.batting = ma.parse_ball_by_ball(load("cheetahs_batting.txt"))
        cls.bowling = ma.parse_ball_by_ball(load("cheetahs_bowling.txt"))

    def test_no_warnings(self):
        self.assertEqual(self.batting.warnings, [])
        self.assertEqual(self.bowling.warnings, [])

    def test_batting_totals(self):
        stats = ma.batting_stats(self.batting)
        self.assertEqual(
            {k: (v["runs"], v["balls"], v["fours"], v["sixes"]) for k, v in stats.items()},
            {
                "Maniraj Durairaj": (41, 35, 3, 0),
                "Srivatsan Sundararajan": (50, 34, 6, 1),
                "Varun Thomas": (16, 14, 0, 1),
            },
        )

    def test_opposition_batting(self):
        stats = ma.batting_stats(self.bowling)
        self.assertEqual(
            {k: (v["runs"], v["balls"]) for k, v in stats.items()},
            {
                "Anand RK": (15, 15), "Neeraj Vaish": (6, 10), "Vinay Aski": (16, 21),
                "Adhavan Saravanamani": (16, 20), "Anoop Sreenath": (8, 13),
                "Abhinandhan Narayanan": (13, 17), "Ajay Bisht": (26, 20),
                "Sidharth Mishra": (0, 1), "Bharadwaja Dasari": (1, 3),
            },
        )

    def test_bowling_figures(self):
        stats = ma.bowling_stats(self.bowling)
        self.assertEqual(
            {k: (v["overs"], v["maidens"], v["runs"], v["wickets"]) for k, v in stats.items()},
            {
                "Sankar Piramanayagam": ("4", 1, 11, 1), "Henry Gengiti": ("3", 0, 22, 0),
                "Srivatsan Srinivasan": ("3", 0, 27, 1), "Divya Patel": ("4", 0, 17, 2),
                "Rajesh Ravikumar": ("3", 0, 18, 1), "Sujith Kumaar": ("3", 0, 13, 1),
            },
        )

    def test_partial_over_figures(self):
        stats = ma.bowling_stats(self.batting)
        self.assertEqual(stats["Prabhat Kumar"]["overs"], "1.5")
        self.assertEqual(stats["Prabhat Kumar"]["runs"], 12)

    def test_same_short_name_resolves_by_role(self):
        self.assertIn("Srivatsan Sundararajan", ma.batting_stats(self.batting))
        self.assertIn("Srivatsan Srinivasan", ma.bowling_stats(self.bowling))


class TimingTests(unittest.TestCase):
    STREAM_START_UTC = 1793549960  # 16:19:20 UTC, 08:19:20 at the ground

    def test_clock_start_ignores_pc_timezone(self):
        clock_start = ma.find_clock_start(self.STREAM_START_UTC, 8 * 60 + 52, 11 * 60 + 43, 12963)
        self.assertEqual(clock_start, 8 * 3600 + 19 * 60 + 20)

    def test_first_ball_logged_in_the_stream_start_minute(self):
        # Nov 7 2026: stream began 12:48:08 PST, first ball logged as 12:48.
        stream_start = 1794084488
        clock_start = ma.find_clock_start(stream_start, 12 * 60 + 48, 16 * 60 + 10, 12491)
        self.assertEqual(clock_start, 12 * 3600 + 48 * 60 + 8)

    def test_wrong_video_is_rejected(self):
        with self.assertRaises(ValueError):
            ma.find_clock_start(self.STREAM_START_UTC, 8 * 60, 11 * 60, 600)

    def make_balls(self, minutes):
        return [
            ma.Ball("0.1", 0, "A", "B", 0, 0, 0, True, True, False, False, False, False, "", m)
            for m in minutes
        ]

    def test_two_balls_in_one_minute_get_their_own_changes(self):
        balls = self.make_balls([10, 10, 11])
        self.assertEqual(ma.align_balls(balls, [610.0, 640.0, 675.0], 0), [610.0, 640.0, 675.0])

    def test_extra_scoreboard_changes_are_skipped(self):
        balls = self.make_balls([10, 12])
        self.assertEqual(ma.align_balls(balls, [100.0, 620.0, 700.0, 745.0], 0), [620.0, 745.0])

    def test_ball_without_a_change_is_left_empty(self):
        balls = self.make_balls([10, 20])
        self.assertEqual(ma.align_balls(balls, [630.0], 0), [630.0, None])


class WrongMatchTests(unittest.TestCase):
    COUGARS = ("C:/Users/vatsu/Downloads/YTDown.com_YouTube_Media_z8byewKtMN8_Cheetahs-2026-Winter-"
               "T20-WB-Cheetahs-Alpha-vs-Cougars-Black-11-01-2026_001_1080p.mp4")
    TRACY_PAGE = ("Completed\n172/6\n20/20\nvs\n101/10\n14.2/20\nCheetahs Alpha\nTracy Centurions\n"
                  "League\n|\n\nNov 8, 2026\n")

    def test_teams_from_the_copied_page(self):
        self.assertEqual(ma.parse_match_teams(self.TRACY_PAGE), ["Cheetahs Alpha", "Tracy Centurions"])

    def test_teams_from_the_link(self):
        link = ("https://cricclubs.com/NCCA/results/EnQ9PTYJEBL1zDJ8Gu8I8w"
                "?asfd=Cheetahs+Alpha+vs+Tracy+Centurions&tab=ball_by_ball")
        self.assertEqual(ma.parse_match_teams("no header here", link), ["Cheetahs Alpha", "Tracy Centurions"])

    def test_cougars_video_with_tracy_scorecard_is_caught(self):
        date = ma.parse_match_date(self.TRACY_PAGE)
        teams = ma.parse_match_teams(self.TRACY_PAGE)
        self.assertEqual(ma.video_name_mismatch(self.COUGARS, teams, date), ["teams", "date"])

    def test_right_video_passes(self):
        name = ("YTDown.com_YouTube_Media_9uMonVVXcXQ_Cheetahs-2026-Winter-T20-WB-Strikers-Blue-2-vs-"
                "Cheetahs-Beta-11-07-2026_001_1080p.mp4")
        teams = ["Strikers Blue 2", "Cheetahs Beta"]
        self.assertEqual(ma.video_name_mismatch(name, teams, ma.parse_match_date("Nov 7, 2026")), [])

    def test_name_that_says_nothing_passes(self):
        self.assertEqual(ma.video_name_mismatch("C:/videos/match.mp4", ["A", "B"],
                                                ma.parse_match_date("Nov 8, 2026")), [])


class OversBoxTests(unittest.TestCase):
    """The overs number read from real overlay images (Nov 7 2026, 1080p)."""

    @classmethod
    def setUpClass(cls):
        import json
        with open(os.path.join(HERE, "fixtures", "overs_box_nov7.json"), encoding="utf-8") as f:
            cls.samples = json.load(f)["samples"]

    def test_every_sample_reads_right(self):
        for sample in self.samples:
            self.assertEqual(ma.read_overs_text(bytes.fromhex(sample["bitmap"])), sample["text"])

    def make_run(self, start, text):
        bitmap = next(bytes.fromhex(s["bitmap"]) for s in self.samples if s["text"] == text)
        return (start, start - 5, bitmap)

    def test_each_legal_ball_gets_the_change_before_its_overs_showed(self):
        balls = TimingTests.make_balls(None, [10, 11, 12])
        for ball, label in zip(balls, ("2.1", "2.6", "3.1")):
            ball.over_label = label
        runs = [self.make_run(100.0, "2.1"), self.make_run(160.0, "3")]
        moments, unsure = ma.overs_moments(balls, runs, [97.0, 140.0, 158.5, 200.0])
        self.assertEqual(moments[:2], [97.0, 158.5])
        self.assertEqual(unsure, {2})  # 3.1 never showed, so it's estimated

    def test_reentered_ball_is_unsure(self):
        balls = TimingTests.make_balls(None, [10])
        balls[0].over_label = "2.1"
        runs = [self.make_run(100.0, "2.1"), self.make_run(160.0, "3"), self.make_run(220.0, "2.1")]
        _, unsure = ma.overs_moments(balls, runs, [99.0])
        self.assertEqual(unsure, {0})


class BallQueueTests(unittest.TestCase):
    CLOCK = 13 * 3600 + 7 * 60 + 41  # scorer's clock at the video's first frame

    def make_match(self, seen_every=5):
        """Two innings of 120 balls, 38 s apart with longer over breaks and a
        20-minute break. Every `seen_every`-th ball is missed by the queue, and
        a few stray paths are added. (With 1 in 3 missed, the whole match can
        slip by one ball, which is why the movement sequence fills the gaps.)"""
        import random
        rng = random.Random(7)
        clock_times, t = [], self.CLOCK + 300
        for innings in range(2):
            for ball in range(120):
                clock_times.append(t)
                t += 38 + rng.randint(-6, 6) + (55 if ball % 6 == 5 else 0)
            t += 1200
        balls = TimingTests.make_balls(None, [int(c // 60) for c in clock_times])
        truth = [c - self.CLOCK for c in clock_times]
        deliveries = [v for i, v in enumerate(truth) if i % seen_every]
        deliveries += [truth[40] + 15, truth[150] + 19, 30.0]  # throws, a warm-up
        return balls, truth, sorted(deliveries), t - self.CLOCK + 300

    def as_queue(self, deliveries):
        return [[moment, True] for moment in deliveries]

    def test_queue_lines_up_without_the_stream_start(self):
        balls, truth, deliveries, duration = self.make_match()
        clock = ma.fit_queue_to_scorecard(balls, self.as_queue(deliveries), duration)
        moments, exact = ma.queue_moments(balls, self.as_queue(deliveries), clock)
        seen_right = sum(1 for m, s, t in zip(moments, exact, truth) if s and abs(m - t) < 1)
        seen = sum(exact)
        self.assertGreaterEqual(seen_right / seen, 0.95)

    def test_missed_ball_is_placed_between_its_neighbours(self):
        balls, truth, deliveries, duration = self.make_match()
        clock = ma.fit_queue_to_scorecard(balls, self.as_queue(deliveries), duration)
        moments, exact = ma.queue_moments(balls, self.as_queue(deliveries), clock)
        self.assertFalse(exact[5])  # every fifth ball was missed
        self.assertTrue(moments[4] < moments[5] < moments[6])

    def test_wrong_video_is_rejected(self):
        balls, _, _, duration = self.make_match()
        with self.assertRaises(ValueError):
            ma.fit_queue_to_scorecard(balls, self.as_queue([100.0, 400.0]), duration)

    def test_strong_sequence_beats_stray_movement_in_the_same_minute(self):
        balls = TimingTests.make_balls(None, [10])
        queue = [[605.0, False, 0.42], [640.0, True, 0.9]]
        moments, exact = ma.queue_moments(balls, queue, 0)
        self.assertEqual((moments, exact), ([640.0], [True]))

    def test_sequence_without_a_ball_path_is_kept_but_unsure(self):
        patterns_from = [100.0 + 40 * i for i in range(10)]
        motion = [0] * (ma.SEQUENCE_ZONES * ma.SEQUENCE_FPS * 600)

        def add_sequence(moment):
            frame = int(moment * ma.SEQUENCE_FPS)
            for offset in range(-12, 0):  # run-up at one end
                motion[(frame + offset) * ma.SEQUENCE_ZONES + 1] = 200
            for offset in range(0, 6):  # the shot at the other end
                motion[(frame + offset) * ma.SEQUENCE_ZONES + 6] = 200

        for moment in patterns_from + [520.0]:
            add_sequence(moment)
        queue = ma.combine_deliveries(patterns_from + [300.5], motion)
        times = [entry[0] for entry in queue]
        self.assertIn(520.0, times)  # the ball was hidden, the movement found it
        self.assertFalse(queue[times.index(520.0)][1])
        self.assertTrue(queue[0][1])
        self.assertNotIn(300.5, [t for t in times if abs(t - 300.5) < 1])  # a lone path: a throw

    def test_one_delivery_per_ball(self):
        paths = [
            ma.BallCandidate(100.0, 8, 300, 100), ma.BallCandidate(101.5, 9, 302, 101),
            ma.BallCandidate(140.0, 7, 298, 99), ma.BallCandidate(150.0, 9, 40, 10),
            ma.BallCandidate(180.0, 8, 301, 100),
        ]
        self.assertEqual(ma.pick_deliveries(paths), [101.5, 140.0, 180.0])

    def test_queue_reads_the_whole_video_in_pieces(self):
        asked = []

        def finder(start, length):
            asked.append((start, length))
            return [ma.BallCandidate(start + 5, 8, 300, 100)]

        found = ma.scan_ball_queue("unused.mp4", 50, ball_finder=finder)
        self.assertEqual(asked, [(0.0, 21), (20.0, 21), (40.0, 10)])
        self.assertEqual(found, [5.0, 25.0, 45.0])


class ClipTimingTests(unittest.TestCase):
    def test_motion_rises_are_strongest_first(self):
        motion = [100] * 21
        motion[8] = 900
        motion[15] = 400
        self.assertEqual(ma.pick_motion_rises(motion, count=2), [8, 15])

    def highlights(self, ball_finder, rise_finder):
        batting = ma.parse_ball_by_ball(load("cheetahs_batting.txt"))
        bowling = ma.parse_ball_by_ball(load("cheetahs_bowling.txt"))
        clock = 8 * 3600 + 19 * 60 + 20
        balls = ma.innings_in_match_order(batting, bowling)
        # Real scoreboard changes are never closer than MIN_SECONDS_BETWEEN_CHANGES.
        changes = []
        for ball in balls:
            expected = ball.minute_of_day * 60 + 30 - clock
            changes.append(max(expected, changes[-1] + 10) if changes else expected)
        return ma.build_highlights(batting, bowling, clock, changes, ball_finder, rise_finder)

    def test_found_ball_gives_four_second_clip(self):
        finder = lambda start, length: [ma.BallCandidate(start + 10, 8, 300, 60)]
        for highlight in self.highlights(finder, None):
            self.assertEqual(highlight.end - highlight.start, 6 if highlight.watermark == "W" else 4)
            self.assertTrue(highlight.exact)

    def test_ball_not_found_keeps_clip_length_and_is_flagged(self):
        for rise_finder in (lambda start, length: [start + 5], lambda start, length: [], None):
            for highlight in self.highlights(lambda start, length: [], rise_finder):
                self.assertEqual(highlight.end - highlight.start, 6 if highlight.watermark == "W" else 4)
                self.assertFalse(highlight.exact)


class BallCandidateTests(unittest.TestCase):
    def test_learns_the_usual_path_from_clear_balls(self):
        lists = [
            [ma.BallCandidate(1, 9, 300, 60)], [ma.BallCandidate(2, 8, 310, 55)],
            [ma.BallCandidate(3, 7, 290, 65)], [ma.BallCandidate(4, 5, 90, 200)],
        ]
        self.assertEqual(ma.find_ball_path_centre(lists), (300, 60))

    def test_too_few_clear_balls_gives_no_path(self):
        self.assertIsNone(ma.find_ball_path_centre([[ma.BallCandidate(1, 9, 300, 60)]]))

    def test_faint_path_needs_movement_at_the_same_time(self):
        faint = ma.BallCandidate(100, 3, 320, 60)
        centre = (300, 60)
        self.assertIs(ma.pick_weak_ball_candidate([faint], [101], centre, 90), faint)
        self.assertIsNone(ma.pick_weak_ball_candidate([faint], [110], centre, 90))

    def test_faint_path_right_after_the_previous_ball_is_ignored(self):
        faint = ma.BallCandidate(92, 3, 320, 60)
        self.assertIsNone(ma.pick_weak_ball_candidate([faint], [92], (300, 60), 90))

    def test_earliest_backed_faint_path_wins(self):
        first = ma.BallCandidate(100, 3, 320, 60)
        later = ma.BallCandidate(104, 4, 320, 60)
        self.assertIs(ma.pick_weak_ball_candidate([later, first], [101, 104], (300, 60), 90), first)

    def test_strong_path_off_the_usual_line_is_rejected(self):
        wrong = ma.BallCandidate(1, 12, 500, 180)
        right = ma.BallCandidate(2, 6, 310, 70)
        self.assertIs(ma.pick_ball_candidate([wrong, right], (300, 60)), right)
        self.assertIsNone(ma.pick_ball_candidate([wrong], (300, 60)))


class BallPathTests(unittest.TestCase):
    WIDTH = 640

    def frames(self, dots_by_frame, count=30):
        return [dots_by_frame.get(i, []) for i in range(count)]

    def test_finds_a_straight_falling_path(self):
        dots = {i: [(320 - 9 * (i - 5), 20 + 10 * (i - 5))] for i in range(5, 12)}
        path = ma.pick_ball_path(self.frames(dots), self.WIDTH)
        self.assertEqual([frame for frame, _ in path], list(range(5, 12)))

    def test_survives_one_missed_frame(self):
        dots = {i: [(320 - 9 * (i - 5), 20 + 10 * (i - 5))] for i in (5, 6, 7, 9, 10, 11)}
        self.assertEqual(len(ma.pick_ball_path(self.frames(dots), self.WIDTH)), 6)

    def test_ignores_short_paths(self):
        dots = {i: [(320 - 9 * (i - 5), 20 + 10 * (i - 5))] for i in range(5, 8)}
        self.assertIsNone(ma.pick_ball_path(self.frames(dots), self.WIDTH))

    def test_ignores_sideways_movement(self):
        dots = {i: [(100 + 12 * i, 200 + i)] for i in range(5, 14)}
        self.assertIsNone(ma.pick_ball_path(self.frames(dots), self.WIDTH))

    def test_ignores_upward_movement(self):
        dots = {i: [(320 - 9 * (i - 5), 200 - 10 * (i - 5))] for i in range(5, 12)}
        self.assertIsNone(ma.pick_ball_path(self.frames(dots), self.WIDTH))


class ScorecardLinkTests(unittest.TestCase):
    def test_link_is_pointed_at_ball_by_ball(self):
        url = ma.ball_by_ball_url("https://site.org/NCCA/results/abc?asfd=A+vs+B&tab=over_by_over")
        self.assertTrue(url.endswith("tab=ball_by_ball"))
        self.assertNotIn("over_by_over", url)
        self.assertIn("asfd=A+vs+B", url)

    def test_link_without_a_tab_gets_one(self):
        self.assertIn("tab=ball_by_ball", ma.ball_by_ball_url("https://site.org/results/abc"))

    def test_text_that_is_not_a_link_is_rejected(self):
        with self.assertRaises(ValueError):
            ma.ball_by_ball_url("not a link")


class MatchVideoTests(unittest.TestCase):
    TEAMS = ["Cheetahs Beta", "Livermore Monks"]
    DATE = ma.parse_match_date("League\n|\nNov 1, 2026\nCheetahs Beta won by 9 Wickets")
    RIGHT = "Cheetahs - 2026 Winter T20 WB - Cheetahs Beta vs Livermore Monks - 11/01/2026"

    def test_reads_short_and_full_month_names(self):
        self.assertEqual(str(self.DATE), "2026-11-01")
        self.assertEqual(str(ma.parse_match_date("played on March 8, 2026 here")), "2026-03-08")

    def test_no_date_gives_none(self):
        self.assertIsNone(ma.parse_match_date("Runs : 3"))

    def test_search_text_has_both_teams_and_the_date(self):
        self.assertEqual(
            ma.video_search_text(self.TEAMS, self.DATE), "Cheetahs Beta vs Livermore Monks 11/01/2026"
        )

    def test_right_video_matches(self):
        self.assertTrue(ma.is_match_video(self.RIGHT, self.TEAMS, self.DATE))

    def test_other_date_formats_match(self):
        for title in ("Cheetahs Beta v Livermore Monks, Nov 1, 2026", "livermore monks - cheetahs beta 11-1-26"):
            self.assertTrue(ma.is_match_video(title, self.TEAMS, self.DATE), title)

    def test_same_teams_on_another_day_are_rejected(self):
        title = "Cheetahs Cricket - Livermore Monks vs Cheetahs Beta, Mar 8, 2026"
        self.assertFalse(ma.is_match_video(title, self.TEAMS, self.DATE))
        self.assertFalse(ma.is_match_video(self.RIGHT.replace("11/01", "11/11"), self.TEAMS, self.DATE))

    def test_a_date_inside_another_date_is_rejected(self):
        self.assertFalse(ma.is_match_video("Cheetahs Beta vs Livermore Monks 11/1/20261", self.TEAMS, self.DATE))
        self.assertFalse(ma.is_match_video("Cheetahs Beta vs Livermore Monks 111/1/2026", self.TEAMS, self.DATE))

    def test_other_team_is_rejected(self):
        title = self.RIGHT.replace("Livermore Monks", "UC Cubs")
        self.assertFalse(ma.is_match_video(title, self.TEAMS, self.DATE))

    def test_full_recording_beats_a_short_highlight(self):
        results = [
            {"title": self.RIGHT + " highlights", "duration": 90, "id": "short"},
            {"title": self.RIGHT, "duration": 12963, "id": "full"},
            None,
            {"title": "Something else", "duration": 99999, "id": "other"},
        ]
        self.assertEqual(ma.pick_match_video(results, self.TEAMS, self.DATE)["id"], "full")

    def test_no_match_gives_none(self):
        self.assertIsNone(ma.pick_match_video([{"title": "Other"}], self.TEAMS, self.DATE))

class AssignInningsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batted = load("cheetahs_batting.txt")
        cls.bowled = load("cheetahs_bowling.txt")

    def test_order_of_copying_doesnt_matter(self):
        for first, second in ((self.batted, self.bowled), (self.bowled, self.batted)):
            batting, bowling, problem = ma.assign_innings(first, second, "Cheetahs")
            self.assertIs(batting, self.batted)
            self.assertIs(bowling, self.bowled)
            self.assertIsNone(problem)

    def test_works_when_the_other_team_is_ours(self):
        batting, bowling, problem = ma.assign_innings(self.batted, self.bowled, "Livermore")
        self.assertIs(batting, self.bowled)
        self.assertIsNone(problem)

    def test_team_name_case_is_ignored(self):
        batting, _, _ = ma.assign_innings(self.bowled, self.batted, "cheetahs beta")
        self.assertIs(batting, self.batted)

    def test_unknown_team_is_reported(self):
        self.assertIsNotNone(ma.assign_innings(self.batted, self.bowled, "Tigers")[2])

    def test_no_squad_lists_is_reported(self):
        self.assertIsNotNone(ma.assign_innings(self.batted, self.batted, "Cheetahs")[2])


class ClipRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batting = ma.parse_ball_by_ball(load("cheetahs_batting.txt"))
        cls.bowling = ma.parse_ball_by_ball(load("cheetahs_bowling.txt"))

    def test_every_delivery_of_both_innings_is_requested(self):
        requests = ma.every_ball_requests(self.batting, self.bowling)
        self.assertEqual(len(requests), len(self.batting.balls) + len(self.bowling.balls))

    def test_batting_clips_are_short_and_bowling_clips_are_long(self):
        for request in ma.every_ball_requests(self.batting, self.bowling):
            self.assertEqual(request.long_clip, request.group == "Bowling")

    def test_every_ball_clips_name_the_right_player(self):
        requests = ma.every_ball_requests(self.batting, self.bowling)
        batting = [r.player for r in requests if r.group == "Batting"]
        bowling = [r.player for r in requests if r.group == "Bowling"]
        self.assertIn("Varun Thomas", batting)
        self.assertIn("Divya Patel", bowling)
        self.assertNotIn("Divya Patel", batting)

    def test_every_ball_watermarks_match_the_ball(self):
        marks = [r.watermark for r in ma.every_ball_requests(self.batting, self.bowling) if r.group == "Bowling"]
        self.assertEqual(marks.count("W"), 8)  # includes the two run outs

    def test_highlights_are_grouped_by_discipline(self):
        groups = [r.group for r in ma.highlight_requests(self.batting, self.bowling)]
        self.assertEqual((groups.count("Batting"), groups.count("Bowling")), (11, 6))

    def test_one_innings_alone_works(self):
        self.assertEqual(len(ma.every_ball_requests(self.batting, None)), len(self.batting.balls))


class ReelCommandTests(unittest.TestCase):
    def test_reel_uses_a_widely_playable_colour_format(self):
        segments = [
            {"path": "clip.mp4", "duration": 7.0, "has_audio": True},
            {"path": "card.mp4", "duration": 3.0, "has_audio": False},
        ]
        cmd = ma.build_reel_cmd(segments, "reel.mp4")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")


class HighlightTests(unittest.TestCase):
    def test_counts_only_our_boundaries_and_bowler_wickets(self):
        batting = ma.parse_ball_by_ball(load("cheetahs_batting.txt"))
        bowling = ma.parse_ball_by_ball(load("cheetahs_bowling.txt"))
        highlights = ma.build_highlights(batting, bowling, 8 * 3600 + 19 * 60 + 20, [])
        kinds = [h.watermark for h in highlights]
        self.assertEqual((kinds.count("4"), kinds.count("6"), kinds.count("W")), (9, 2, 6))
        self.assertFalse(any(h.exact for h in highlights))


class ParserGuardTests(unittest.TestCase):
    def test_empty_paste_warns(self):
        self.assertTrue(ma.parse_ball_by_ball("Share\nnothing here").warnings)

    def test_missing_ball_is_reported(self):
        text = load("cheetahs_batting.txt").replace(
            "2\n0.5\nGopi A to Srivatsan S, 2 runs\n10:45 AM\n", ""
        )
        warnings = ma.parse_ball_by_ball(text).warnings
        self.assertTrue(any("over 1" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
