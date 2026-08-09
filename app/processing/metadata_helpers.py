import re


PEOPLE_SPLIT_RE = re.compile(r"[,，、]")


def split_people(value: str) -> list:
    return [item.strip() for item in PEOPLE_SPLIT_RE.split(value or "") if item.strip()]


def join_people_for_tag(value: str) -> str:
    return ";".join(split_people(value))


def join_people_for_folder(value: str) -> str:
    return "&".join(split_people(value))


def format_anchor_for_folder(anchor: str) -> str:
    folder_anchor = join_people_for_folder(anchor)
    if folder_anchor and not folder_anchor.startswith("演播"):
        return f"演播{folder_anchor}"
    return folder_anchor


def build_output_folder_name(
    album_title: str,
    author: str,
    anchor: str,
    finished: str,
    year: str,
    format_part: str,
    bitrate_part: str,
    team: str = "RL",
) -> str:
    team_suffix = f" -{team.strip()}" if (team or "").strip() else ""
    folder_author = join_people_for_folder(author)
    display_anchor = format_anchor_for_folder(anchor)
    status = finished if finished else "未知状态"
    return f"{album_title} - {folder_author} - {display_anchor} - {status} - {year} - {format_part} {bitrate_part}{team_suffix}"
