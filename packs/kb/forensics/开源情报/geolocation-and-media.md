---
title: 地理定位与媒体分析
phase: forensics
vuln_class: [osint, geolocation, image, exif, map]
summary: 图题的判据是「有没有可读文字」——有字就先 OCR 定国，无字就反向图搜定城，定到城再靠微观地标（电线杆/护栏/铺装）钉到 3 米方格。
---

# 地理定位与媒体分析

账号与帖子见 [[forensics/开源情报/social-media.md|社交媒体]]；域名/IP/仓库见 [[forensics/开源情报/web-and-dns.md|Web 与 DNS]]；总入口见 [[forensics/开源情报/index.md|开源情报]]。

## 0. 判据速查

| 观察到的现象 | 结论 | 去哪节 |
|---|---|---|
| 图里有招牌/路牌/店名 | 先裁切再 OCR，文字是最快的国别判据 | §2 |
| 文字左右反了 | 水面/玻璃镜像，水平翻转 | §3 |
| 整图搜不出但局部特征强 | 裁到只留招牌/门面再搜 | §2 |
| 需要判国家 | 行驶侧 + 文字系统 + 路牌样式 | §4 |
| 已定城市，要钉到点 | 微观地标匹配（杆/石墩/铺装/花坛） | §5 |
| 图是全景照片的一块 | Street View 特征点匹配 | §6 |
| 坐标格式不认识 | MGRS / Plus Code / W3W 三种 | §7 |
| 需要「某类店在某站旁」 | Overpass `around` 查询 | §8 |
| flag 是三个英文单词 | What3Words，3 米精度 | §7 |
| 一堆图各藏一个字符 | 多地点 + 二级编码 | §9 |

## 1. 起手顺序

1. `exiftool` —— 有 GPS 就直接结束。但**主流社交平台会剥离 EXIF**（Twitter 尤其彻底），缺失不代表被刻意清除，别在这上面耗。
2. 反向图搜 —— Google Lens / Images、TinEye（精确同图）、Yandex（人脸、东欧）、Bing Visual。**中国场景优先百度 `graph.baidu.com`**（蓝牌、简体、门楼建筑是信号）。
3. 视觉特征 —— 建筑、植被、地形、车牌、文字。
4. **视觉隐写** —— 全分辨率看**四角与四边**，找暗底暗字/亮底亮字、字号递减的文本。头像与封面图是高发位置。

## 2. 裁切再搜

整图搜经常返回「风景照」这类泛化结果；裁到招牌后能直接返回具体商家与地址。

- 店面 → 只留门面与招牌
- 地标 → 只留最有辨识度的建筑特征
- 路牌 → 只留文字与路线编号
- 教堂/大楼 → 只留独特立面

**Google Lens 对裁切区域的效果显著优于整图**，这是免费且最有效的单步提升。

## 3. 镜像文字

水面、玻璃、镜面里的字是反的。水平翻转后按正常文字读；只读到 60-70% 时用引号做部分匹配。

```bash
convert input.jpg -flop flipped.jpg
python3 -c "from PIL import Image; Image.open('input.jpg').transpose(Image.FLIP_LEFT_RIGHT).save('flipped.jpg')"
```

```text
"Aguas de Lind"          # 引号强制部分匹配
"Aguas de Lind" city     # 加语境词
```

**字母歧义（`T` vs `I`）要把两种变体都搜一遍**，镜像会让笔画细节失真。

## 4. 判国家：三个独立信号

**行驶侧** + **文字系统** + **路牌样式**，三者互相印证，任一单独都可能误判。

| 特征 | 国家/地区 |
|---|---|
| 汉字 + 蓝色高速牌 | 日本 |
| 西里尔字母 + 宽中央分隔带大道 | 俄罗斯/CIS |
| 白底红边 X 形道口牌 | 加拿大 |
| 黄色菱形警告牌 | 美国/加拿大 |
| 绿色高速牌 | 德国 |
| 棕色旅游牌 | 法国 |
| 带红色反光片的系柱 | 荷兰 |
| 镀锌波形护栏 + 混凝土海堤 + 白色小灯塔渔港 | 日本沿海 |

**后苏联视觉标记**：赫鲁晓夫楼/勃列日涅夫楼式板楼、宽中央分隔带、混凝土公交站、特殊电线杆、苏联时期马赛克壁画。

**品牌定位法**（比建筑更精确）：读出任何店名/连锁 logo → 搜「品牌 + locations/branches」→ 拿到分布图 → 与海岸线/地形交叉，锁定具体分店。

## 5. 从城市钉到 3 米

已知城市但不知具体位置时，靠**微观地标**而不是大建筑：

- 电线杆样式、石墩、护栏、花坛、地砖铺装、垃圾桶、路灯臂
- **背景建筑三角定位**：找出图里背景的几栋楼，在 Street View 里找到同样的楼，反推相机必须站在哪个位置才能得到同样的透视
- 地理特征收窄：先用地貌（湖、河、海岸线）把范围缩到几个街区，再切 Street View

**判据：3 米精度题要求匹配「相机所在位置」，不是「相机对着的东西」**。入口和停车场是两个不同的方格。

## 6. Street View 全景匹配

题图常是全景照片的**裁切块**，可能不含地平线或天空，定位更难。

```python
import cv2
orb = cv2.ORB_create(nfeatures=5000)
kp1, des1 = orb.detectAndCompute(challenge, None)
kp2, des2 = orb.detectAndCompute(candidate, None)
bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
score = sum(1 for m in bf.match(des1, des2) if m.distance < 50)
```

**单一指标会误判**：特征点匹配、局部 patch 比对、颜色直方图三种分数各自排序后**加权合并**，比任何单指标都稳。

```text
GET https://maps.googleapis.com/maps/api/streetview/metadata?location=LAT,LNG&key=KEY   # 查覆盖
GET https://maps.googleapis.com/maps/api/streetview?size=640x480&location=LAT,LNG&heading=90&key=KEY
```

全景 ID 从页面 JS 里找 `panoId`。**覆盖稀疏的地区（格陵兰、冰岛、法罗）直接穷举该区域全部全景**，比猜快。

## 7. 三种坐标格式

| 格式 | 形态 | 精度 | 转换 |
|---|---|---|---|
| MGRS | `4V FH 246 677`（带区带字母+方格+偏移） | 米级 | 在线转换器 → 经纬度 |
| Plus Code | `XXXX+XX` 或 `8FVC9G8F+6W`，字母表 `23456789CFGHJMPQRVWX` | ~14 m | Google Maps 落 pin，详情面板直接给 |
| What3Words | 三个英文单词 | 3 m | what3words.com 输坐标反查 |

**Plus Code 免费无 key**，比 W3W 好用；短码（`XXXX+XX`）需要附近地名补全，这是设计如此不是解析错。**W3W 的相邻方格地址完全无关**（无空间相关性），所以不能靠「猜附近」线性推，只能逐个试 5-10 个相邻方格。

**W3W 常见坑**：

- 建筑入口 vs 停车场是两个方格
- 地址指**相机位置**还是**拍摄对象**，题面要看清
- 卫星图 pin 与 W3W 网格不一定对齐，Street View 对齐相机角度更可靠
- 用 OSM 建筑轮廓做精确校正

## 8. Overpass 空间查询

已知城市 + 「某类店在某类设施旁」时，`around` 把候选从上千缩到几个。

```text
[out:json][timeout:25];
{{geocodeArea:Barcelona}}->.searchArea;
(
  node["railway"="subway_entrance"](area.searchArea);
)->.metros;
(
  node(around.metros:10)["shop"~"newsagent|kiosk"];
  way(around.metros:10)["shop"~"newsagent|kiosk"];
);
out body;
>;
out skel qt;
```

| Tag | 常用值 |
|---|---|
| `shop` | `newsagent` `kiosk` `bakery` `supermarket` |
| `amenity` | `cafe` `restaurant` `bank` `atm` `pharmacy` |
| `tourism` | `hotel` `attraction` `museum` `viewpoint` |
| `railway` | `station` `subway_entrance` `halt` |

**`around` 是唯一值得记的算子**，它把「手动翻地图几小时」变成一次查询。半径给小（10-50 m），大了结果没意义。

## 9. 多地点 + 二级编码

一类题给 14 张图，每张定位出一个「钢琴键号」，串起来是 flag。

**策略**：先把所有地点认完（这步是常规地理定位，不难），再回头看「每个地点给出的那个数字」如何映射——映射规则通常从数据分布就能反推（1-88 就是钢琴键，1-128 是 ASCII，0-127 是 MIDI note）。

**别一边定位一边猜映射**，点位没认全时映射规律看不出来。

## 10. 交叉验证

反向图搜失败时（题图是原创而非抓取），用**地名搜照片**而不是用图搜图：Google Maps 点开地点 → Photos 标签 → 比对建筑、树、路面、水体。公园、广场、地标这类游客照密集的地方命中率很高。

纪念碑字母（西语 `letras monumentales`、葡语 `letreiro turístico`）在拉美城市极常见，常在主广场且带倒影水池，直接搜这个词组 + 城市名。

## 11. 纪律

- **先读字再看景**。可读文字（招牌、路牌、车牌）一步定国甚至定城，比对着植被猜气候快一个数量级。
- **裁切是免费提精度**。整图搜不出时，先裁再搜，不要立刻换工具。
- **精度要求决定终点**。要城市就停在城市，要 3 米就必须做微观地标匹配——用城市级证据去答 3 米题必错。
- **EXIF 缺失是常态不是线索**。社交平台剥元数据是默认行为，别把「没有 EXIF」当成「有人清理过」。
