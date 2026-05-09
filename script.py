from music21 import stream, note, meter, harmony, instrument, layout, metadata, common, duration
from music21.harmony import CHORD_TYPES
import xml.etree.ElementTree as ET
from pathlib import Path
import re, copy

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

def fix_triplet_tuplets_musicxml(path: str) -> None:
    """
    music21 often emits bracket=\"no\" and start+stop tuplets on every triplet member.
    Normalize to one start (bracket yes) on the first 3:2 member and one stop on the last.
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

    def apply_tuplet_span(note_el, role):
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
        tup.set("number", "1")
        if role == "start":
            tup.set("type", "start")
            tup.set("bracket", "yes")
        else:
            tup.set("type", "stop")

    for measure in root.iter():
        if _xml_local_tag(measure) != "measure":
            continue
        triplet_notes = [ch for ch in measure if _xml_local_tag(ch) == "note" and is_triplet_3_2(ch)]
        for i in range(0, len(triplet_notes), 3):
            group = triplet_notes[i : i + 3]
            if len(group) != 3:
                continue
            for n in group:
                strip_tuplets(n)
            apply_tuplet_span(group[0], "start")
            apply_tuplet_span(group[1], "mid")
            apply_tuplet_span(group[2], "stop")

    tree.write(path, encoding="utf-8", xml_declaration=True)

def make_measure(tokens):
    """
    Build one 4/4 measure using equal subdivision logic:
    1 token = whole note
    2 tokens = half notes
    4 tokens = quarters, etc.
    """
    m = stream.Measure()

    n = len(tokens)
    if n == 0:
        return m

    beat_length = 4.0 / n
    tpl_template = None
    if n == 3:
        tpl_template = duration.Tuplet(numberNotesActual=3, numberNotesNormal=2)
        tpl_template.setDurationType("half")
        tpl_template.bracket = True  # MusicXML: request a bracket (default is True, but be explicit)
    offset = 0.0
    for idx, tok in enumerate(tokens):

        # REST
        if tok in ["-", ",", "r", "R"]:
            r = note.Rest()
            r.quarterLength = beat_length
            if tpl_template is not None:
                tpl = copy.deepcopy(tpl_template)
                tpl.type = "start" if idx == 0 else "stop" if idx == n - 1 else None
                r.duration.appendTuplet(tpl)
            m.insert(offset, r)

        # CHORD (piano harmony only)
        else:
            bass = None
            if "/" in tok:
                splitted_tok = tok.split("/")
                tok = splitted_tok[0]
                bass = splitted_tok[1] if len(splitted_tok) > 1 else None
            else:
                bass = None

            # h = harmony.ChordSymbol(tok, bass=bass)
            """
            """
            figure_for_parse = common.cleanedFlatNotation(normalize_chord_figure(tok))  # e.g. …m7b5…
            h = harmony.ChordSymbol(figure_for_parse, bass=bass)
            # if "m7b5" in figure_for_parse or h.chordKind in (
            #     "half-diminished-seventh",
            #     "half-diminished",
            # ):
            #     print("ø7")
            #     h.chordKindStr = "ø7"
            # else:
            #     print(h)
            """
            """
            m.insert(offset, h)

            # silent rhythmic anchor (keeps alignment stable)
            r = note.Rest()
            r.quarterLength = beat_length
            if tpl_template is not None:
                tpl = copy.deepcopy(tpl_template)
                tpl.type = "start" if idx == 0 else "stop" if idx == n - 1 else None
                r.duration.appendTuplet(tpl)
            m.insert(offset, r)

        offset += beat_length

    return m




def build_score(text, filename, transpose=0):
    score = stream.Score()
    part = stream.Part()
    part.insert(0, instrument.Piano())
    part.append(meter.TimeSignature("4/4"))

    md = metadata.Metadata()
    md.title = filename
    md.composer = "S Ha"   # common shortcut for composer field
    score.metadata = md

    measure_number = 1
    last_harmony: str | None = None
    for line_index, line in enumerate(text.splitlines(), start=1):
        if "|" not in line:
            continue
        groups = parse_line(line)
        for group_index, tokens in enumerate(groups):
            try:
                tokens, last_harmony = expand_slash_continuation(tokens, last_harmony)
                m = make_measure(tokens)
            except Exception as e:
                raise RuntimeError(
                    f"{filename}: line {line_index}, group {group_index}, tokens={tokens!r}"
                ) from e
            m.number = measure_number

            # Force a new system at the start of each line
            if group_index == 0:
                m.insert(0, layout.SystemLayout(isNew=True))

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
        fix_triplet_tuplets_musicxml(str(out_path))
        print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()