import os
import json
import random
import subprocess
import asyncio
import urllib.parse
import datetime

import requests
from groq import Groq
import edge_tts
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

WIDTH, HEIGHT = 1080, 1920
FPS = 25
NUM_SCENES = 6
TRANSITION = 0.4

VOICES = ["en-US-GuyNeural", "en-US-EricNeural", "en-GB-RyanNeural"]

PERF_FILE = "video_performance.json"
BAN_DAYS = 60
BOOST_VIEWS = 1000
FAIL_VIEWS = 300

CATEGORY_ROTATION = ["space_science", "history_althistory", "disaster_mystery_survival"]

TOPIC_POOLS = {
    "space_science": "space, astronomy, physics, the solar system, black holes, the sun, the moon, future technology",
    "history_althistory": "world history, alternate history, ancient civilizations, historical what-if scenarios",
    "disaster_mystery_survival": "natural disasters, earth mysteries, survival scenarios, dinosaurs, unexplained phenomena",
}

IMAGE_STYLE = (
    "Ultra Detailed Documentary Illustration, Cinematic Lighting, "
    "Realistic Digital Painting, Dark Atmospheric Environment, "
    "Consistent Character Design, Professional Storytelling Art, "
    "High Detail, Movie Quality, Dramatic Shadows, Volumetric Lighting, "
    "Realistic Backgrounds, YouTube Documentary Style, no text, no watermark"
)


def run_ffmpeg(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("=== FFMPEG COMMAND FAILED ===")
        print("CMD:", " ".join(cmd))
        print("--- STDERR ---")
        print(result.stderr[-3000:])
        print("=== END FFMPEG ERROR ===")
        raise RuntimeError("ffmpeg command failed, see log above")
    return result


def extract_json(content):
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    content = content.strip()
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found: {content[:300]}")
    return json.loads(content[start:end + 1])


def call_groq(client, prompt, temperature=1.0):
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    content = response.choices[0].message.content
    print("=== RAW MODEL OUTPUT (first 400 chars) ===")
    print(content[:400])
    return extract_json(content)


# ---------- Performance tracking ----------
def load_performance():
    if os.path.exists(PERF_FILE):
        with open(PERF_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"videos": []}


def save_performance(data):
    with open(PERF_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_video_stats(perf):
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        return perf
    now = datetime.datetime.utcnow()
    ids_to_check = []
    for v in perf["videos"]:
        if v.get("views_checked"):
            continue
        uploaded = datetime.datetime.fromisoformat(v["uploaded_at"])
        if (now - uploaded).days >= 2:
            ids_to_check.append(v["video_id"])
    if not ids_to_check:
        return perf
    for i in range(0, len(ids_to_check), 50):
        batch = ids_to_check[i:i + 50]
        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "statistics", "id": ",".join(batch), "key": api_key},
                timeout=30,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            views_by_id = {it["id"]: int(it["statistics"].get("viewCount", 0)) for it in items}
        except Exception as e:
            print(f"Stats fetch failed: {e}")
            continue
        for v in perf["videos"]:
            if v["video_id"] in views_by_id:
                v["views"] = views_by_id[v["video_id"]]
                v["views_checked"] = True
    save_performance(perf)
    return perf


def get_topic_guidance(perf, category):
    now = datetime.datetime.utcnow()
    topic_stats = {}
    for v in perf["videos"]:
        if not v.get("views_checked") or v.get("category") != category:
            continue
        topic_stats.setdefault(v["topic"], []).append(v)

    banned, boosted = [], []
    for topic, vids in topic_stats.items():
        views_list = [v["views"] for v in vids]
        last_date = max(datetime.datetime.fromisoformat(v["uploaded_at"]) for v in vids)
        if max(views_list) >= BOOST_VIEWS:
            boosted.append(topic)
        elif len(vids) >= 2 and max(views_list) < FAIL_VIEWS:
            if (now - last_date).days < BAN_DAYS:
                banned.append(topic)
    return banned, boosted


def pick_category_for_run():
    now = datetime.datetime.utcnow()
    index = now.hour % len(CATEGORY_ROTATION)
    return CATEGORY_ROTATION[index]


# ---------- Content generation ----------
def generate_script(category, topics_used, banned_topics, boosted_topics):
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    topic_hint = TOPIC_POOLS[category]

    boost_line = (
        f"Topics that performed well before, try similar angles: {', '.join(boosted_topics[-5:])}."
        if boosted_topics else ""
    )
    ban_line = (
        f"Do NOT write about these topics, they underperformed: {', '.join(banned_topics)}."
        if banned_topics else ""
    )

    prompt = f"""You are the lead content director for a viral "What If" YouTube Shorts channel
called "What If Tomorrow", targeting US, Canada, UK, and Australia audiences.

Write ONE short video script (35-50 seconds when narrated) about a "What If" scenario
from this topic area: {topic_hint}.

Mandatory rules:
- Start with NO intro, NO greeting. Open immediately with a shocking "What if...?" hook question.
- Fast, thrilling, curiosity-driven narration style.
- The first 2 seconds must be an irresistible hook.
- Do NOT ask the viewer to subscribe or like.
- Structure across exactly 6 scenes:
  1. Hook (the "What if" question itself, shocking)
  2. Immediate consequence
  3. Escalation (things get stranger/more intense)
  4. Escalation continues, more dramatic
  5. Climax reveal (the most shocking part)
  6. Final twist or thought-provoking closing line
- Each scene caption should be short, punchy, 8-14 words.

{boost_line}
{ban_line}
Do not repeat any of these topics already used: {', '.join(topics_used[-40:]) if topics_used else 'none'}.

Also write:
- title: short, clickable, professional title starting with "What If"
- topic: 2-4 word topic name for tracking (e.g. "earth stops spinning")
- description: short YouTube description (2-3 sentences)
- hashtags: 5 relevant hashtags

Return ONLY valid JSON in exactly this shape, no extra text, no markdown:
{{"title": "...", "topic": "...", "description": "...", "hashtags": ["h1","h2","h3","h4","h5"], "scenes": [
{{"caption": "..."}}, {{"caption": "..."}}, {{"caption": "..."}}, {{"caption": "..."}}, {{"caption": "..."}}, {{"caption": "..."}}
]}}
"""
    return call_groq(client, prompt, temperature=1.05)


def generate_image_prompts(data, num_scenes):
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    captions_text = "\n".join(f"{i+1}. {s['caption']}" for i, s in enumerate(data["scenes"]))

    prompt = f"""These are the scenes of a "What If" YouTube Shorts video:
{captions_text}

Write {num_scenes} image prompts in English, one per scene in the same order,
depicting each scene visually and specifically (not vague or generic).
Each character or subject should stay visually consistent across prompts.
No text or writing should appear in the image.

Return ONLY valid JSON in exactly this shape, no extra text:
{{"image_prompts": ["...", "...", "... (exactly {num_scenes} total)"]}}
"""
    return call_groq(client, prompt, temperature=0.85)


def generate_full_content(perf, category):
    topics_used = []
    if os.path.exists("used_topics.json"):
        with open("used_topics.json", "r") as f:
            topics_used = json.load(f)

    banned_topics, boosted_topics = get_topic_guidance(perf, category)
    print("Category:", category)
    print("Banned topics:", banned_topics)
    print("Boosted topics:", boosted_topics)

    script_data = generate_script(category, topics_used, banned_topics, boosted_topics)
    images_data = generate_image_prompts(script_data, NUM_SCENES)

    data = {
        "title": script_data["title"],
        "topic": script_data["topic"],
        "category": category,
        "description": script_data.get("description", ""),
        "hashtags": script_data.get("hashtags", []),
        "scenes": script_data["scenes"],
        "image_prompts": images_data["image_prompts"],
    }

    topics_used.append(data["topic"])
    with open("used_topics.json", "w") as f:
        json.dump(topics_used, f, ensure_ascii=False)

    return data


# ---------- Image generation ----------
def download_image(prompt, out_path):
    full_prompt = f"{prompt}, {IMAGE_STYLE}"
    seed = random.randint(1, 999999)
    encoded = urllib.parse.quote(full_prompt)
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1080&height=1920&seed={seed}&nologo=true&model=flux&enhance=true"
    )
    try:
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        img = Image.open(out_path).convert("RGB")
        img = img.resize((WIDTH, HEIGHT))
        img.save(out_path)
    except Exception as e:
        print(f"Image download failed: {e}")
        img = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 25))
        img.save(out_path)


# ---------- Voice + word timings ----------
async def synthesize_with_timings(text, voice, audio_out):
    communicate = edge_tts.Communicate(text, voice, rate="+2%")
    words = []
    with open(audio_out, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                dur = chunk["duration"] / 1e7
                words.append({"text": chunk["text"], "start": start, "end": start + dur})
    return words


def get_audio_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


# ---------- ASS captions ----------
def to_ass_time(seconds):
    seconds = max(seconds, 0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def clean_word(w):
    return w.replace("{", "").replace("}", "").strip()


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,105,&HFFFFFF&,&HFFFFFF&,&H000000&,&H000000&,1,0,0,0,100,100,0,0,3,7,0,5,60,60,760,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_scene_ass(words, ass_path, max_chunk_words=4, max_chunk_chars=24):
    lines = [ASS_HEADER]
    if words:
        chunks = []
        current, current_chars = [], 0
        for w in words:
            text = clean_word(w["text"])
            if not text:
                continue
            if current and (len(current) >= max_chunk_words or current_chars + len(text) > max_chunk_chars):
                chunks.append(current)
                current, current_chars = [], 0
            current.append(w)
            current_chars += len(text) + 1
        if current:
            chunks.append(current)

        for chunk in chunks:
            for idx, w in enumerate(chunk):
                line_start = w["start"]
                line_end = chunk[idx + 1]["start"] if idx < len(chunk) - 1 else w["end"] + 0.15
                parts = []
                for j, cw in enumerate(chunk):
                    word_text = clean_word(cw["text"])
                    if j == idx:
                        parts.append("{\\c&H00D7FF&}" + word_text + "{\\c&HFFFFFF&}")
                    else:
                        parts.append(word_text)
                full_text = " ".join(parts)
                pop = "{\\fscx55\\fscy55\\t(0,140,\\fscx100\\fscy100)}" if idx == 0 else ""
                lines.append(
                    f"Dialogue: 0,{to_ass_time(line_start)},{to_ass_time(line_end)},"
                    f"Default,,0,0,0,,{pop}{full_text}\n"
                )
    with open(ass_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def ffmpeg_escape_path(path):
    abs_path = os.path.abspath(path)
    return abs_path.replace("\\", "\\\\").replace(":", "\\:")


# ---------- Video building ----------
def make_scene_video(image_path, ass_path, duration, out_path, direction=0):
    frames = max(int(duration * FPS), FPS)
    zoom_in = direction % 2 == 0
    zoom_expr = "min(zoom+0.004,1.5)" if zoom_in else "if(lte(zoom,1.0),1.5,max(1.0,zoom-0.004))"
    pans = [
        ("iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        ("0", "0"),
        ("iw-iw/zoom", "0"),
        ("0", "ih-ih/zoom"),
    ]
    px, py = pans[direction % len(pans)]
    safe_ass = ffmpeg_escape_path(ass_path)
    vf = (
        f"scale=1600:2844,"
        f"zoompan=z='{zoom_expr}':d={frames}:x='{px}':y='{py}':s={WIDTH}x{HEIGHT}:fps={FPS},"
        f"subtitles='{safe_ass}'"
    )
    run_ffmpeg(
        ["ffmpeg", "-y", "-loop", "1", "-i", image_path,
         "-vf", vf, "-t", str(duration),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path]
    )


def crossfade_concat(clip_paths, durations, out_path, transition=TRANSITION):
    n = len(clip_paths)
    if n == 1:
        run_ffmpeg(["ffmpeg", "-y", "-i", clip_paths[0], "-c", "copy", out_path])
        return durations[0]
    inputs = []
    for p in clip_paths:
        inputs += ["-i", p]
    filter_parts = []
    cumulative = durations[0]
    prev_label = "0"
    for i in range(1, n):
        offset = cumulative - transition
        out_label = f"v{i}" if i < n - 1 else "vout"
        filter_parts.append(
            f"[{prev_label}][{i}]xfade=transition=fade:duration={transition}:offset={offset:.3f}[{out_label}]"
        )
        cumulative = cumulative + durations[i] - transition
        prev_label = out_label
    filter_complex = ";".join(filter_parts)
    run_ffmpeg(
        ["ffmpeg", "-y"] + inputs +
        ["-filter_complex", filter_complex, "-map", "[vout]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path]
    )
    return cumulative


def pad_video_to_duration(video_in, target_duration, video_out):
    current = get_audio_duration(video_in)
    deficit = target_duration - current
    if deficit <= 0.05:
        run_ffmpeg(["ffmpeg", "-y", "-i", video_in, "-c", "copy", video_out])
        return
    run_ffmpeg(
        ["ffmpeg", "-y", "-i", video_in,
         "-vf", f"tpad=stop_mode=clone:stop_duration={deficit:.3f}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", video_out]
    )


def concat_audio(file_list, list_path, out_path):
    with open(list_path, "w") as f:
        for path in file_list:
            f.write(f"file '{os.path.abspath(path)}'\n")
    run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
         "-c:a", "libmp3lame", "-q:a", "2", out_path]
    )


def merge_audio_video(video_path, audio_path, out_path):
    run_ffmpeg(
        ["ffmpeg", "-y", "-i", video_path, "-i", audio_path,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", out_path]
    )


def upload_to_youtube(video_path, title, description):
    creds = Credentials(
        None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["shorts", "whatif", "facts"],
            "categoryId": "27",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    video_id = response.get("id")
    print("Uploaded video ID:", video_id)
    print(f"Video URL: https://youtube.com/watch?v={video_id}")
    return video_id


def main():
    os.makedirs("scenes", exist_ok=True)

    perf = load_performance()
    perf = update_video_stats(perf)

    category = pick_category_for_run()

    print("1) Generating content...")
    data = generate_full_content(perf, category)
    print("Title:", data["title"])

    voice = random.choice(VOICES)
    print("Voice:", voice)

    scene_videos = []
    scene_audios = []
    scene_durations = []

    for i, scene in enumerate(data["scenes"]):
        print(f"Scene {i+1}: generating audio...")
        audio_path = f"scenes/audio_{i}.mp3"
        words = asyncio.run(synthesize_with_timings(scene["caption"], voice, audio_path))
        duration = get_audio_duration(audio_path)
        scene_audios.append(audio_path)
        scene_durations.append(duration)

        print(f"Scene {i+1}: generating image...")
        img_path = f"scenes/raw_{i}.jpg"
        download_image(data["image_prompts"][i], img_path)

        print(f"Scene {i+1}: building captions...")
        ass_path = f"scenes/caps_{i}.ass"
        build_scene_ass(words, ass_path)

        print(f"Scene {i+1}: rendering clip ({duration:.1f}s)...")
        clip_path = f"scenes/clip_{i}.mp4"
        make_scene_video(img_path, ass_path, duration, clip_path, direction=i)
        scene_videos.append(clip_path)

    print("Combining scenes...")
    total_audio_duration = sum(scene_durations)
    crossfade_concat(scene_videos, scene_durations, "combined_video.mp4")

    print("Syncing duration...")
    pad_video_to_duration("combined_video.mp4", total_audio_duration, "synced_video.mp4")

    print("Combining audio...")
    concat_audio(scene_audios, "scenes/audio_list.txt", "combined_audio.mp3")

    print("Merging audio and video...")
    merge_audio_video("synced_video.mp4", "combined_audio.mp3", "output.mp4")

    print("Uploading to YouTube...")
    hashtags_line = " ".join(f"#{h}" for h in data.get("hashtags", []))
    description = f"{data['description']}\n\n{hashtags_line}"
    video_id = upload_to_youtube("output.mp4", data["title"] + " #Shorts", description)

    perf["videos"].append({
        "video_id": video_id,
        "topic": data["topic"],
        "category": category,
        "title": data["title"],
        "uploaded_at": datetime.datetime.utcnow().isoformat(),
        "views_checked": False,
        "views": 0,
    })
    save_performance(perf)

    print("Done!")


if __name__ == "__main__":
    main()
