"""Reviewed-in-code starter prompts; human language/audio review is still required."""
# code, TTS voice, sky question, blue pattern, grass question, green pattern
ROWS = [
    ('en', 'en', 'Ember, what color is a clear daytime sky?', r'blue', 'And what color is grass usually?', r'green'),
    ('es', 'es', 'Ember, ¿de qué color es el cielo despejado durante el día?', r'azul', '¿Y de qué color suele ser la hierba?', r'verde'),
    ('fr', 'fr', 'Ember, de quelle couleur est le ciel dégagé pendant la journée ?', r'bleu', 'Et de quelle couleur est généralement l’herbe ?', r'vert'),
    ('de', 'de', 'Ember, welche Farbe hat ein klarer Himmel am Tag?', r'blau', 'Und welche Farbe hat Gras normalerweise?', r'grün'),
    ('it', 'it', 'Ember, di che colore è il cielo sereno durante il giorno?', r'azzurr|blu', 'E di che colore è di solito l’erba?', r'verd'),
    ('pt-BR', 'pt-br', 'Ember, qual é a cor do céu limpo durante o dia?', r'azul', 'E qual costuma ser a cor da grama?', r'verd'),
    ('pt-PT', 'pt-pt', 'Ember, de que cor é o céu limpo durante o dia?', r'azul', 'E de que cor costuma ser a relva?', r'verd'),
    ('hi', 'hi', 'एम्बर, साफ़ दिन में आसमान का रंग कैसा होता है?', r'नील|आसमानी', 'और घास आम तौर पर किस रंग की होती है?', r'हर[ाीे]'),
    ('ja', 'ja', 'エンバー、晴れた昼間の空は何色ですか？', r'青|あお|ブルー', 'では、草は普通何色ですか？', r'緑|みどり|グリーン'),
    ('ko', 'ko', '엠버, 맑은 낮의 하늘은 무슨 색인가요?', r'파[란랑랗]|푸른|푸르', '그러면 풀은 보통 무슨 색인가요?', r'초록|녹색'),
    ('zh-Hans', 'cmn', 'Ember，晴朗的白天天空是什么颜色？', r'蓝|藍', '那草通常是什么颜色？', r'绿|綠'),
    ('zh-Hant', 'cmn', 'Ember，晴朗的白天天空是什麼顏色？', r'藍|蓝', '那草通常是什麼顏色？', r'綠|绿'),
    ('ar', 'ar', 'إمبر، ما لون السماء الصافية في النهار؟', r'أزرق|زرق', 'وما لون العشب عادة؟', r'أخضر|خضر'),
    ('ru', 'ru', 'Эмбер, какого цвета ясное дневное небо?', r'голуб|син', 'А какого цвета обычно трава?', r'зел[её]н'),
    ('bn', 'bn', 'এমবার, পরিষ্কার দিনে আকাশের রং কী?', r'নীল', 'আর ঘাস সাধারণত কী রঙের হয়?', r'সবুজ'),
    ('nl', 'nl', 'Ember, welke kleur heeft een heldere hemel overdag?', r'blauw', 'En welke kleur heeft gras meestal?', r'groen'),
    ('id', 'id', 'Ember, apa warna langit yang cerah pada siang hari?', r'biru', 'Lalu apa warna rumput biasanya?', r'hijau'),
    ('mr', 'mr', 'एम्बर, स्वच्छ दिवशी आकाशाचा रंग कोणता असतो?', r'निळ|नीळ', 'आणि गवताचा रंग साधारणपणे कोणता असतो?', r'हिरव'),
    ('pl', 'pl', 'Ember, jaki kolor ma bezchmurne niebo w ciągu dnia?', r'niebies|błękit', 'A jaki kolor ma zazwyczaj trawa?', r'zielon'),
    ('ro', 'ro', 'Ember, ce culoare are cerul senin în timpul zilei?', r'albastr', 'Și ce culoare are de obicei iarba?', r'verde'),
    ('ta', 'ta', 'எம்பர், தெளிவான பகலில் வானம் என்ன நிறத்தில் இருக்கும்?', r'நீல', 'புல் பொதுவாக என்ன நிறத்தில் இருக்கும்?', r'பச்சை'),
    ('te', 'te', 'ఎంబర్, పగటిపూట నిర్మలమైన ఆకాశం ఏ రంగులో ఉంటుంది?', r'నీల', 'మరి గడ్డి సాధారణంగా ఏ రంగులో ఉంటుంది?', r'ఆకుపచ్చ|పచ్చ'),
    ('th', 'th', 'เอ็มเบอร์ ท้องฟ้าในวันที่อากาศแจ่มใสเป็นสีอะไร', r'ฟ้า|น้ำเงิน', 'แล้วหญ้าปกติเป็นสีอะไร', r'เขียว'),
    ('tr', 'tr', 'Ember, açık bir gündüz gökyüzü ne renktir?', r'mavi', 'Peki çimen genellikle ne renktir?', r'yeşil'),
    ('uk', 'uk', 'Ембер, якого кольору ясне денне небо?', r'блакит|син', 'А якого кольору зазвичай трава?', r'зелен'),
    ('vi', 'vi', 'Ember, bầu trời quang đãng ban ngày có màu gì?', r'xanh', 'Vậy cỏ thường có màu gì?', r'xanh lá|xanh lục'),
]


def scenarios():
    cases = []
    for code, voice, sky, blue, grass, green in ROWS:
        steps = [dict(text=text, tts_voice=voice, expected_languages=[code],
                      expect=dict(reply=pattern, no_tools=True, max_latency_s=15))
                 for text, pattern in ((sky, blue), (grass, green))]
        steps.append(dict(text='Ember, what color is a ripe banana usually?', tts_voice='en',
                          expected_languages=['en'], expect=dict(reply='yellow', no_tools=True, max_latency_s=15)))
        cases.append(dict(name=code, language=code, about='Natural response, follow-up and switch to English.', steps=steps))
    cases.append(dict(name='hi-en-mixed', language='hi', mixed=True,
        about='Hindi-English mixed speech; human review must check both languages, not just a keyword.',
        steps=[dict(text='Ember, आज sky बहुत blue है। तुम्हें कैसा लगता है?', tts_voice='hi',
                    expected_languages=['hi', 'en'], expect=dict(no_tools=True, max_latency_s=15)),
               dict(text='Ember, अब सिर्फ हिंदी में बताओ कि घास किस रंग की होती है।', tts_voice='hi',
                    expected_languages=['hi'], expect=dict(reply='हर[ाीे]', no_tools=True, max_latency_s=15))]))
    return cases


if __name__ == '__main__':
    import json
    from pathlib import Path
    Path(__file__).with_name('scenarios.json').write_text(
        json.dumps(scenarios(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
