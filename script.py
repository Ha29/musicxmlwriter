from music21 import stream, note, meter, harmony, instrument, layout, metadata, common, duration, expressions, tempo
from music21.harmony import CHORD_TYPES
import xml.etree.ElementTree as ET
from pathlib import Path
import re, copy, string

# print(CHORD_TYPES)

def normalize_chord_figure(tok):
    """Parse-friendly figures; ø is unreliable inside ChordSymbol for some slash chords."""
    s = tok
    if "ø7" in s:
        s = s.replace("ø7", "m7b5")
    elif "ø" in s:
        s = s.replace("ø", "m7b5")
    return s
def set_chord_kind_display(h):
    """
    Override how the quality prints in MusicXML (<kind text="…">).
    Semantics stay in chordKind / pitches; this is display only.
    """
    kind = h.chordKind
    text = {
        "diminished": "°",
        "diminished-seventh": "°7",
        "half-diminished-seventh": "ø7",
        "half-diminished": "ø7",
        "augmented": "+",
    }.get(kind)
    if text is not None:
        h.chordKindStr = text
def parse_line(line):
    """Split a system line into bar groups and tokens."""
    return [
        [tok.strip() for tok in group.split() if tok.strip()]
        for group in line.split("|")
        if group.strip()
    ]
def expand_slash_continuation(tokens: list[str], last_harmony: str | None) -> tuple[list[str], str | None]:
    """Expand '/G#'-style tokens; return new token list and updated last harmony string."""
    out: list[str] = []
    for tok in tokens:
        if tok in ["-", ",", "r", "R"]:
            out.append(tok)
            continue
        if "/" in tok:
            left, right = tok.split("/", 1)
            if left == "":
                if last_harmony is None:
                    raise ValueError(
                        f"Slash continuation {tok!r} has no prior chord (use a full chord first)."
                    )
                tok = f"{last_harmony}/{right}"
            last_harmony = tok.split("/", 1)[0]
        else:
            last_harmony = tok
        out.append(tok)
    return out, last_harmony
def _xml_local_tag(elem):
    tag = elem.tag
    return tag.split("}")[-1] if tag.startswith("{") else tag


# Triplet grid: n slots per 4/4 bar → n/3 groups of 3:2 tuplets (triplet quarters, eighths, …).
# Requires n divisible by 3 and n/3 a power of two ≥ 2 (e.g. 6 → 2× quarter triplets, 12 → 4× eighth triplets).
_TRIPLET_GRID_NORMAL_TYPES = ("quarter", "eighth", "16th", "32nd", "64th")


def triplet_grid_normal_type(n: int) -> str | None:
    """If tokens form an equal triplet grid, return music21 `setDurationType` string; else None."""
    if n < 6 or n % 3 != 0:
        return None
    groups = n // 3
    if groups < 2 or (groups & (groups - 1)):
        return None
    ti = groups.bit_length() - 2
    if ti < 0 or ti >= len(_TRIPLET_GRID_NORMAL_TYPES):
        return None
    return _TRIPLET_GRID_NORMAL_TYPES[ti]


def triplet_grid_tuplet(idx: int, normal_type: str) -> duration.Tuplet:
    """One member of a 3:2 tuplet; group index from idx//3 for MusicXML tuplet number."""
    pos = idx % 3
    group = idx // 3
    tpl = duration.Tuplet(numberNotesActual=3, numberNotesNormal=2)
    tpl.setDurationType(normal_type)
    tpl.bracket = True
    tpl.tupletId = group + 1
    if pos == 0:
        tpl.type = "start"
    elif pos == 2:
        tpl.type = "stop"
    else:
        tpl.type = None
    return tpl


def fix_half_diminished_musicxml(path: str) -> None:
    """
    music21 exports m7b5 as <kind>minor-seventh</kind> + <degree> flat 5.
    MuseScore/OSMD build 'm7b5' from that; they ignore kind@text='ø7'.
    Rewrite to MusicXML half-diminished and drop the redundant degree.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag.startswith("{"):
        ns_uri = root.tag.split("}")[0][1:]
        ET.register_namespace("", ns_uri)
    for harmony in root.iter():
        if _xml_local_tag(harmony) != "harmony":
            continue
        kind_el = None
        degree_children = []
        for child in list(harmony):
            t = _xml_local_tag(child)
            if t == "kind":
                kind_el = child
            elif t == "degree":
                degree_children.append(child)
        if kind_el is None:
            continue
        kind_body = (kind_el.text or "").strip().lower()
        if kind_body == "augmented":
            kind_el.set("text", "+")
            kind_el.set("use-symbols", "yes")
            continue
        if kind_body == "major-seventh":
            kind_el.set("text", "M7")
            kind_el.set("use-symbols", "yes")
            continue
        if kind_body != "minor-seventh":
            continue
        def is_flat_fifth_degree(deg):
            dv = da = None
            for el in deg.iter():
                lt = _xml_local_tag(el)
                if lt == "degree-value":
                    dv = (el.text or "").strip()
                elif lt == "degree-alter":
                    da = (el.text or "").strip()
            return dv == "5" and da == "-1"
        to_remove = [d for d in degree_children if is_flat_fifth_degree(d)]
        if not to_remove:
            continue
        kind_el.text = "half-diminished"
        kind_el.set("text", "ø7")
        kind_el.set("use-symbols", "yes")
        for d in to_remove:
            harmony.remove(d)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def _note_duration_int(note_el):
    for ch in note_el:
        if _xml_local_tag(ch) == "duration":
            try:
                return int((ch.text or "").strip())
            except ValueError:
                return None
    return None


def _note_has_rest(note_el):
    return any(_xml_local_tag(ch) == "rest" for ch in note_el)


def _measure_full_rest_note(note_el):
    for ch in note_el:
        if _xml_local_tag(ch) == "rest" and ch.get("measure") == "yes":
            return True
    return False


def _time_modification_triplet_3_2(note_el):
    tm = None
    for ch in note_el:
        if _xml_local_tag(ch) == "time-modification":
            tm = ch
            break
    if tm is None:
        return False
    an = nn = None
    for ch in tm:
        t = _xml_local_tag(ch)
        if t == "actual-notes":
            an = (ch.text or "").strip()
        elif t == "normal-notes":
            nn = (ch.text or "").strip()
    return an == "3" and nn == "2"


def _strip_all_notations(note_el):
    for ch in list(note_el):
        if _xml_local_tag(ch) == "notations":
            note_el.remove(ch)


def fix_split_merged_half_rest_in_triplet_measures_musicxml(path: str) -> None:
    """
    music21 sometimes merges three triplet-member rests into one half rest without
    time-modification. Split those lumps back into three 3:2 rests so tuplets stay aligned.
    Run after fix_half_diminished_musicxml, before fix_triplet_tuplets_musicxml.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag.startswith("{"):
        ns_uri = root.tag.split("}")[0][1:]
        ET.register_namespace("", ns_uri)

    for measure in root.iter():
        if _xml_local_tag(measure) != "measure":
            continue

        templates = {}
        for ch in list(measure):
            if _xml_local_tag(ch) != "note":
                continue
            if not _note_has_rest(ch) or not _time_modification_triplet_3_2(ch):
                continue
            d = _note_duration_int(ch)
            if d is not None and d not in templates:
                templates[d] = ch

        if not templates:
            continue

        splits = []
        for ch in list(measure):
            if _xml_local_tag(ch) != "note":
                continue
            if not _note_has_rest(ch) or _measure_full_rest_note(ch):
                continue
            if _time_modification_triplet_3_2(ch):
                continue
            big = _note_duration_int(ch)
            if big is None:
                continue
            tmpl = None
            for d_small, tnote in templates.items():
                if big == 3 * d_small:
                    tmpl = tnote
                    break
            if tmpl is not None:
                splits.append((ch, tmpl))

        for old_note, tmpl in splits:
            ix = list(measure).index(old_note)
            measure.remove(old_note)
            for off in range(3):
                nn = copy.deepcopy(tmpl)
                _strip_all_notations(nn)
                measure.insert(ix + off, nn)

    tree.write(path, encoding="utf-8", xml_declaration=True)


def fix_triplet_tuplets_musicxml(path: str) -> None:
    """
    music21 often emits bracket=\"no\" and start+stop tuplets on every triplet member.
    Normalize to one start (bracket yes) on the first 3:2 member and one stop on the last.
    Walk notes in measure order: each three consecutive 3:2 notes form one tuplet; non-triplet
    notes break the chain (supports multiple tuplets per measure, e.g. n=6 or n=12 grids).
    """
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag.startswith("{"):
        ns_uri = root.tag.split("}")[0][1:]
        ET.register_namespace("", ns_uri)

    def find_tm(note_el):
        for ch in note_el:
            if _xml_local_tag(ch) == "time-modification":
                return ch
        return None

    def is_triplet_3_2(note_el):
        tm = find_tm(note_el)
        if tm is None:
            return False
        an = nn = None
        for ch in tm:
            t = _xml_local_tag(ch)
            if t == "actual-notes":
                an = (ch.text or "").strip()
            elif t == "normal-notes":
                nn = (ch.text or "").strip()
        return an == "3" and nn == "2"

    def tuplet_children(notations_el):
        return [ch for ch in list(notations_el) if _xml_local_tag(ch) == "tuplet"]

    def strip_tuplets(note_el):
        for ch in list(note_el):
            if _xml_local_tag(ch) != "notations":
                continue
            for tup in tuplet_children(ch):
                ch.remove(tup)
            if len(ch) == 0:
                note_el.remove(ch)

    def apply_tuplet_span(note_el, role, number_str: str):
        """role: 'start' | 'mid' | 'stop'"""
        notations_el = None
        for ch in note_el:
            if _xml_local_tag(ch) == "notations":
                notations_el = ch
                break
        if role == "mid":
            if notations_el is not None:
                for tup in tuplet_children(notations_el):
                    notations_el.remove(tup)
                if len(notations_el) == 0:
                    note_el.remove(notations_el)
            return
        if notations_el is None:
            notations_el = ET.SubElement(note_el, "notations")
        else:
            for tup in tuplet_children(notations_el):
                notations_el.remove(tup)
        tup = ET.SubElement(notations_el, "tuplet")
        tup.set("number", number_str)
        if role == "start":
            tup.set("type", "start")
            tup.set("bracket", "yes")
        else:
            tup.set("type", "stop")

    for measure in root.iter():
        if _xml_local_tag(measure) != "measure":
            continue
        buf: list = []
        tuplet_number = 1

        def flush_group():
            nonlocal buf, tuplet_number
            if len(buf) != 3:
                buf.clear()
                return
            for n_el in buf:
                strip_tuplets(n_el)
            num = str(tuplet_number)
            apply_tuplet_span(buf[0], "start", num)
            apply_tuplet_span(buf[1], "mid", num)
            apply_tuplet_span(buf[2], "stop", num)
            tuplet_number += 1
            buf.clear()

        for ch in measure:
            if _xml_local_tag(ch) != "note":
                continue
            if is_triplet_3_2(ch):
                buf.append(ch)
                if len(buf) == 3:
                    flush_group()
            else:
                buf.clear()

        flush_group()

    tree.write(path, encoding="utf-8", xml_declaration=True)

def make_measure(tokens):
    m = stream.Measure()
    tokens = list(tokens)

    signature_ts = None
    if "(4/4)" in tokens:
        signature_ts = meter.TimeSignature("4/4")
        tokens.remove("(4/4)")
    elif "(2/4)" in tokens:
        signature_ts = meter.TimeSignature("2/4")
        tokens.remove("(2/4)")

    n = len(tokens)
    if n == 0:
        return m

    if signature_ts is not None:
        m.insert(0, signature_ts)

    beat_length = 4.0 / n
    # if signature_ts == meter.TimeSignature("4/4"):
    #     beat_length = 4.0/n
    if signature_ts == meter.TimeSignature("2/4"):
        beat_length = 2.0/n
    grid_normal_type = triplet_grid_normal_type(n)
    tpl_template = None
    if n == 3:
        tpl_template = duration.Tuplet(numberNotesActual=3, numberNotesNormal=2)
        tpl_template.setDurationType("half")
        tpl_template.bracket = True

    offset = 0.0
    for idx, tok in enumerate(tokens):

        if tok in ["-", ",", "r", "R"]:
            r = note.Rest()
            r.quarterLength = beat_length
            if grid_normal_type is not None:
                r.duration.appendTuplet(triplet_grid_tuplet(idx, grid_normal_type))
            elif tpl_template is not None:
                tpl = copy.deepcopy(tpl_template)
                tpl.type = "start" if idx == 0 else "stop" if idx == n - 1 else None
                r.duration.appendTuplet(tpl)
            m.insert(offset, r)

        else:
            bass = None
            if "/" in tok:
                splitted_tok = tok.split("/", 1)
                tok = splitted_tok[0]
                bass = splitted_tok[1] if len(splitted_tok) > 1 else None

            figure_for_parse = common.cleanedFlatNotation(normalize_chord_figure(tok))
            h = harmony.ChordSymbol(figure_for_parse, bass=bass)
            m.insert(offset, h)

            r = note.Rest()
            r.quarterLength = beat_length
            if grid_normal_type is not None:
                r.duration.appendTuplet(triplet_grid_tuplet(idx, grid_normal_type))
            elif tpl_template is not None:
                tpl = copy.deepcopy(tpl_template)
                tpl.type = "start" if idx == 0 else "stop" if idx == n - 1 else None
                r.duration.appendTuplet(tpl)
            m.insert(offset, r)

        offset += beat_length

    return m

FOUR_FOUR_SIGNATURE = meter.TimeSignature("4/4")
TWO_FOUR_SIGNATURE = meter.TimeSignature("2/4")

def build_score(text, filename, transpose=0):
    section_letters = list(string.ascii_uppercase)
    section_letter_index = 0

    score = stream.Score()
    part = stream.Part()
    part.insert(0, instrument.Piano())
    part.append(meter.TimeSignature("4/4"))

    tempo_mark = tempo.MetronomeMark(number=120)


    md = metadata.Metadata()
    md.title = filename
    md.composer = "S Ha"   # common shortcut for composer field
    score.metadata = md

    measure_number = 1
    last_harmony: str | None = None
    # New system after a blank line (or at start of chart); consecutive lines stay on one system.
    new_system_next = True
    system_header = None
    tempo_added = False
    score_tempo = 120
    for line_index, line in enumerate(text.splitlines(), start=1):
        # if "(4/4)" in line:
        #     signature = FOUR_FOUR_SIGNATURE
        #     line = line.replace("(4/4)", "")
        # elif "(2/4)" in line:
        #     signature = TWO_FOUR_SIGNATURE
        #     line = line.replace("(2/4)", "")
        if "|" not in line:
            if not line.strip():
                new_system_next = True
            elif new_system_next and ":" in line and 'Tempo:' not in line:
                system_header = line
            elif 'Tempo:' in line:
                try:
                    score_tempo = int(line.split('Tempo:')[1].strip())
                except (IndexError, ValueError):
                    score_tempo = 120   # fallback default tempo
                tempo_mark = tempo.MetronomeMark(number=score_tempo)
            continue
            
        groups = parse_line(line)
        for group_index, tokens in enumerate(groups):
            try:
                tokens, last_harmony = expand_slash_continuation(tokens, last_harmony)
                m = make_measure(tokens)
                if not tempo_added:
                    m.insert(0, tempo_mark)
                    tempo_added = True
            except Exception as e:
                raise RuntimeError(
                    f"{filename}: line {line_index}, group {group_index}, tokens={tokens!r}"
                ) from e
            m.number = measure_number
            # if signature is not None:
                # m.insert(0, signature)
            if group_index == 0 and new_system_next:
                m.insert(0, layout.SystemLayout(isNew=True))
                if system_header:
                    m.insert(0, expressions.RehearsalMark(section_letters[section_letter_index] + " | " + system_header))
                    # tx = expressions.TextExpression(system_header)
                    # m.insert(0, tx)
                    system_header = ''
                    section_letter_index += 1
                new_system_next = False
            part.append(m)
            measure_number += 1

    score.append(part)

    if transpose != 0:
        score = score.transpose(transpose)

    return score

def transpose_from_filename(path: Path) -> int | None:
    stem = path.stem
    m = re.fullmatch(r"(.+)_([-+]?\d+)", stem)
    if not m:
        return None
    return int(m.group(2))

# ----------------------------
# INPUT (your chord grid)
# ----------------------------
# input_text = """
# C Cø7/Bb | Aaug - F GM7 | C G | Am - F G
# C F C G | Dm7 G7 Cadd9 - | C C G G | Am - F Gb13
# """

# ----------------------------
# BUILD + EXPORT
# ----------------------------
# score = build_score(input_text, transpose=4)

# output_file = "piano_output.musicxml"
# score.write("musicxml", fp=output_file)

# fix_half_diminished_musicxml(output_file)

# print(f"Wrote {output_file}")

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_DIR = SCRIPT_DIR / "input"
OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_TRANSPOSE = 0
def collect_input_files():
    """
    Chord charts under input/: prefer *.txt; if none, use any regular file
    (includes extensionless names like 'input/test').
    Skips hidden files and directories.
    """
    if not INPUT_DIR.is_dir():
        return []
    # txts = sorted(INPUT_DIR.glob("*.txt"))
    # if txts:
    #     return txts
    return sorted(
        p
        for p in INPUT_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )

def main():
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    paths = collect_input_files()
    if not paths:
        print(f"No input files in {INPUT_DIR}")
        return

    for path in paths:
        text = path.read_text(encoding="utf-8")
        transpose = transpose_from_filename(path) or DEFAULT_TRANSPOSE
        score = build_score(text, path.stem, transpose=transpose)
        out_path = OUTPUT_DIR / f"{path.stem}.musicxml"
        score.write("musicxml", fp=str(out_path))
        fix_half_diminished_musicxml(str(out_path))
        fix_split_merged_half_rest_in_triplet_measures_musicxml(str(out_path))
        fix_triplet_tuplets_musicxml(str(out_path))
        print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()