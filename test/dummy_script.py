REQUIREMENTS = []


async def fetch_album(prompt: str, history: list[dict]) -> dict:
    return {
        "album_name": "Kind of Blue",
        "artist": ["Miles Davis"],
        "year": "1959",
        "genre": ["Jazz", "Modal Jazz"],
        "cover_url": "",
        "description": "爵士乐史上最畅销的专辑之一，以调式即兴取代和弦进行，开创了调式爵士乐流派。",
        "listen_tip": "推荐在安静的夜晚戴耳机聆听，专注感受各声部之间的对话。",
    }
