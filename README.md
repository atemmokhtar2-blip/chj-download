---
title: Chj Download
emoji: 🚀
colorFrom: green
colorTo: blue
sdk: gradio
sdk_version: 6.20.0
python_version: '3.12'
app_file: app.py
pinned: true
---

# 🤖 X Downloader Bot - بوت تحميل الوسائط الاحترافي

أقوى بوت على تلغرام لتحميل الفيديوهات، الصور، والمقاطع الصوتية من أكثر من 13 منصة عالمية بجودة عالية جداً وسرعة فائقة.

## ✨ المميزات (Features)
- **🚀 سرعة خارقة:** تحميل ومعالجة الفيديوهات في ثوانٍ معدودة.
- **🎬 دعم منصات متعددة:** (YouTube, TikTok, Instagram, Facebook, Twitter/X, Threads, Reddit, Pinterest, Snapchat, Vimeo, Dailymotion, SoundCloud).
- **🏆 جودة عالية:** دعم تحميل الفيديوهات بجودة تصل إلى 4K و MP3 عالي النقاء.
- **📱 بدون علامة مائية:** تحميل فيديوهات تيك توك وكوايي بدون العلامة المائية المزعجة.
- **🎁 نظام مكافآت:** احصل على نقاط مجانية يومياً ومن خلال دعوة أصدقائك.
- **🎰 عجلة الحظ:** جرب حظك يومياً لتربح نقاط تحميل إضافية.

## 🛠 المنصات المدعومة (Supported Platforms)
- YouTube (Shorts & Videos)
- TikTok (No Watermark)
- Instagram (Reels & Stories)
- Facebook
- Twitter / X
- Pinterest
- Snapchat
- SoundCloud (High Quality Audio)
- والمزيد...

## 🖥 لوحة التحكم الجديدة
تم إضافة **X Downloader Control Center** بدل صفحة الحالة البسيطة. اللوحة تعرض:

- حالة تشغيل البوت ومدة التشغيل.
- عدد المستخدمين والتحميلات اليومية والإجمالية.
- أكثر المنصات استخداماً.
- أحدث عمليات التحميل.
- حالة الكاش، Redis، Media Vault، وحدود الاستخدام.
- خطة تطوير تنفيذية داخلية.
- نظام إعلان تحديث جاهز يرسل لكل المستخدمين رسالة "تم تحديث البوت" مع زر Start.
- محرك تنزيل أقوى بروابط fallback، توسيع الروابط المختصرة، retry متعدد، وإعدادات yt-dlp عالمية أحدث.

تشغيل اللوحة والبوت معاً:

```bash
python3 app.py
```

ثم افتح واجهة Gradio على المنفذ `7860`.

## ⚡ محرك تنزيل أقوى
تمت ترقية محرك التنزيل ليصبح أكثر ثباتاً مع المنصات التي تفشل كثيراً:

- توسيع الروابط المختصرة مثل `youtu.be`, `vt.tiktok.com`, `fb.watch`, `pin.it`, `t.co`.
- تنظيف روابط التتبع مثل `utm_*`, `fbclid`, `igshid`.
- تحويل روابط الموبايل إلى روابط قياسية عندما يكون ذلك آمناً.
- محاولات تنزيل متعددة عبر yt-dlp: default، native HLS، وFFmpeg HLS.
- retry أقوى للـ fragments والـ extractors والملفات.
- Headers عالمية تدعم الإنجليزية والعربية لتقليل الحظر/الفشل الجغرافي.
- fallback تلقائي بين الرابط الأصلي والرابط المنظف والرابط الموسع.
- دعم Cookies اختياري عبر `YTDLP_COOKIES_FILE` أو `YTDLP_COOKIES_FROM_BROWSER` للمنصات الصعبة مثل Instagram/Facebook/YouTube restricted.
- دعم Proxy اختياري عبر `DOWNLOAD_PROXY` عند حظر IP السيرفر في بعض الدول.
- أمر أدمن `/engine_status` لعرض حالة المحرك.
- أمر أدمن `/update_ytdlp` لتحديث yt-dlp بسرعة عند تغيير خوارزميات المنصات.
- fallback مباشر للـ CDN/signed media URL إذا فشل الدمج العادي.
- بنية Engines مستقلة لكل منصة داخل `services/engines/`:
  - `YouTubeEngine`
  - `TikTokEngine`
  - `InstagramEngine`
  - `PinterestEngine`
  - `FacebookEngine`
  - `TwitterEngine`
  - `RedditEngine`
  - `SoundCloudEngine`
  - `GenericEngine`
- اختيار تلقائي للمحرك حسب الدومين مع fallback عام إذا فشل محرك منصة معينة.

### TikTokEnginePro
- يوسّع الروابط المختصرة `vt.tiktok.com` و`vm.tiktok.com` قبل التحليل.
- يشغّل عدة مزودات بالتوازي: TikWM، aweme API، page JSON، yt-dlp، SSSTik، SnapTik، MusicalDown، TikDownloader.
- لا يختار أول نتيجة فقط؛ يقيّم كل نتيجة عبر score يعتمد على no-watermark، جودة الفيديو، صلاحية رابط CDN، والصور في Photo Mode.
- يدعم Photo Mode/Slideshow كألبوم صور بدل اعتباره فيديو فاشل.
- يبدأ التنزيل من رابط CDN المباشر عند توفره، ثم يرجع تلقائيًا إلى yt-dlp عند الحاجة.

### InstagramEnginePro
- يشغّل مزودات متعددة بالتوازي: Web GraphQL، صفحات embed، gallery-dl، و instaloader.
- يقيّم النتائج بنظام score بدل الاكتفاء بأول نتيجة؛ ويستخدم CDN probe للتأكد أن روابط الفيديو/الصور صالحة وليست صفحة login أو JSON خطأ.
- يدعم Reels، المنشورات، الصور، والكاروسيل المختلط صورة/فيديو.
- يضيف تشخيص واضح للحسابات الخاصة أو المحتوى الذي يحتاج cookies/sessionid بدل فشل غامض.
- يمرر `provider_score` و`provider_candidates` للسجلات ولوحة الصيانة لتحديد أفضل مصدر وفشل المزودات بسرعة.

### FacebookEnginePro
- يوسّع روابط `fb.watch` و`fb.com` وروابط الموبايل إلى الوجهة النهائية قبل التحليل.
- يشغّل مزودات متوازية: yt-dlp metadata، HTML/OG parser، و mbasic/mobile parser.
- يستخرج روابط HD/SD المباشرة من مفاتيح فيسبوك مثل `browser_native_hd_url` و`playable_url_quality_hd` و`og:video`.
- يستخدم CDN probe لتجنب روابط login/error واختيار أفضل رابط `fbcdn/fbsbx/scontent` صالح.
- يبدأ التنزيل من CDN المباشر عند توفره، ثم يرجع تلقائيًا إلى yt-dlp العام عند الحاجة.
- يضيف تشخيص `login_required_or_private` عندما يتطلب الفيديو cookies أو يكون خاصًا/محذوفًا.

### TwitterEnginePro
- يدعم `x.com` و`twitter.com` و`t.co` و`fxtwitter.com` و`fixupx.com` و`vxtwitter.com` مع تطبيع الروابط إلى صيغة موحدة.
- يشغّل مزودات متوازية: yt-dlp metadata، واجهات vxtwitter/fxtwitter العامة، و HTML/OpenGraph fallback.
- يدعم فيديوهات X، GIFs المتحركة، الصور، والتغريدات متعددة الوسائط كـ album.
- يختار أفضل variant مباشر من `video.twimg.com` ويفضل MP4 على HLS عندما يكون مناسبًا لتليجرام.
- يستخدم CDN probe لفحص روابط `twimg` قبل إرسالها للمستخدم أو بدء التنزيل المباشر.
- يضيف `provider_score` و`provider_candidates` لتشخيص مصدر النجاح أو سبب الفشل بسرعة.

### RedditEnginePro
- يدعم `reddit.com` و`old.reddit.com` و`new.reddit.com` و`redd.it` و`v.redd.it` مع تطبيع الروابط قبل التحليل.
- يستخدم Reddit JSON API (`.json?raw_json=1`) لاستخراج الفيديوهات والصور والـ galleries والـ crossposts بدون الاعتماد على صفحة HTML فقط.
- يدعم فيديوهات `v.redd.it` وروابط DASH/HLS/fallback MP4، ويفضل MP4 المباشر عندما يكون مناسبًا لتليجرام.
- يدعم صور `i.redd.it` و`preview.redd.it` وألبومات Reddit عبر `gallery_data` و`media_metadata`.
- يشغّل fallback عبر yt-dlp و oEmbed، مع CDN probe للتحقق من روابط Reddit media قبل استخدامها.
- يضيف `provider_score` و`provider_candidates` و`engine_profile` لتشخيص سريع لمصدر النجاح أو سبب الفشل.

## 📢 إرسال إعلان تحديث لكل المستخدمين
من داخل تيليجرام، يستطيع الأدمن إرسال رسالة تحديث جاهزة لكل المستخدمين بإحدى الطريقتين:

1. افتح `/admin` ثم اضغط زر **🚀 إعلان تحديث البوت**.
2. أو استخدم الأمر المباشر:

```text
/announce_update
```

الرسالة تحتوي على ملخص التحديثات وزر **Start / افتح البوت** لإعادة فتح البوت من جديد.

## 🔐 إعداد التوكن بأمان
الأفضل في الإنتاج استخدام متغيرات البيئة:

```bash
export TELEGRAM_BOT_TOKEN="your-token"
export ADMIN_IDS="123456789,987654321"
```

يوجد fallback إلى `token.txt` للتوافق مع النشر القديم، لكن لا يُنصح بالاعتماد عليه في الإنتاج.

## 🚀 ابدأ الآن
يمكنك العثور على البوت على تلغرام عبر المعرف الخاص به والاستمتاع بتجربة تحميل لا مثيل لها.

---
*Built with ❤️ using Python and yt-dlp.*
