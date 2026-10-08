/**
 * lib/emoji.js - built-in emoji data (Unicode Emoji <= 12.1, no Flags category, base glyphs without skin
 * tones) and the picker component (SPEC 9.8).
 *
 * At picker-build time every emoji is checked with a canvas test (its drawn width must differ from that of
 * an unassigned code point); the list of rejected emoji is cached in global storage keyed by the user agent.
 * Recently used emoji live in namespaced storage (`emoji_recent`).
 *
 * Exported API (docs/ui-conv-api.md section 6): QUICK_REACTIONS, getEmojiCategories, searchEmoji,
 * getRecentEmoji, pushRecentEmoji, createEmojiPicker, openEmojiPicker.
 */

import { h } from '../core/dom.js';
import { storage, fold } from '../core/util.js';
import { ui } from '../core/ui.js';

/** The six quick reactions of the message menu (SPEC 9.2). */
export const QUICK_REACTIONS = Object.freeze(['\u{1F44D}', '❤️', '\u{1F602}', '\u{1F62E}', '\u{1F622}', '\u{1F64F}']);

const MAX_RECENT = 32;
const CATEGORY_META = [
  { id: 'smileys', label: 'Smileys & emotion', icon: '\u{1F600}' },
  { id: 'people', label: 'People & body', icon: '\u{1F44B}' },
  { id: 'animals', label: 'Animals & nature', icon: '\u{1F436}' },
  { id: 'food', label: 'Food & drink', icon: '\u{1F354}' },
  { id: 'activities', label: 'Activities', icon: '⚽' },
  { id: 'travel', label: 'Travel & places', icon: '\u{1F697}' },
  { id: 'objects', label: 'Objects', icon: '\u{1F4A1}' },
  { id: 'symbols', label: 'Symbols', icon: '\u{1F523}' },
];

/** One emoji per line: the emoji, a space, lower-case keywords. */
const DATA = {
  smileys: `
😀 grinning face smile
😃 grinning face with big eyes happy
😄 grinning face with smiling eyes
😁 beaming face with smiling eyes grin
😆 grinning squinting face laugh
😅 grinning face with sweat
🤣 rolling on the floor laughing rofl lol
😂 face with tears of joy lol
🙂 slightly smiling face
🙃 upside-down face
😉 winking face wink
😊 smiling face with smiling eyes blush
😇 smiling face with halo angel
🥰 smiling face with hearts love
😍 smiling face with heart-eyes love
🤩 star-struck wow
😘 face blowing a kiss
😗 kissing face
☺ smiling face relaxed
😚 kissing face with closed eyes
😙 kissing face with smiling eyes
😋 face savoring food yum
😛 face with tongue
😜 winking face with tongue
🤪 zany face crazy
😝 squinting face with tongue
🤑 money-mouth face rich
🤗 hugging face hug
🤭 face with hand over mouth oops
🤫 shushing face quiet secret
🤔 thinking face hmm
🤐 zipper-mouth face
🤨 face with raised eyebrow
😐 neutral face
😑 expressionless face
😶 face without mouth
😏 smirking face
😒 unamused face
🙄 face with rolling eyes
😬 grimacing face
🤥 lying face liar
😌 relieved face
😔 pensive face sad
😪 sleepy face
🤤 drooling face
😴 sleeping face zzz
😷 face with medical mask sick
🤒 face with thermometer ill
🤕 face with head-bandage hurt
🤢 nauseated face sick
🤮 face vomiting
🤧 sneezing face
🥵 hot face
🥶 cold face freezing
🥴 woozy face
😵 dizzy face
🤯 exploding head mind blown
🤠 cowboy hat face
🥳 partying face party
😎 smiling face with sunglasses cool
🤓 nerd face
🧐 face with monocle
😕 confused face
😟 worried face
🙁 slightly frowning face
☹ frowning face
😮 face with open mouth wow
😯 hushed face
😲 astonished face
😳 flushed face
🥺 pleading face
😦 frowning face with open mouth
😧 anguished face
😨 fearful face scared
😰 anxious face with sweat
😥 sad but relieved face
😢 crying face tear
😭 loudly crying face sob
😱 face screaming in fear
😖 confounded face
😣 persevering face
😞 disappointed face
😓 downcast face with sweat
😩 weary face
😫 tired face
🥱 yawning face
😤 face with steam from nose
😡 pouting face angry
😠 angry face
🤬 face with symbols on mouth swearing
😈 smiling face with horns devil
👿 angry face with horns
💀 skull
☠ skull and crossbones
💩 pile of poo
🤡 clown face
👹 ogre
👺 goblin
👻 ghost
👽 alien
👾 alien monster
🤖 robot
😺 grinning cat
😸 grinning cat with smiling eyes
😹 cat with tears of joy
😻 smiling cat with heart-eyes
😼 cat with wry smile
😽 kissing cat
🙀 weary cat
😿 crying cat
😾 pouting cat
🙈 see-no-evil monkey
🙉 hear-no-evil monkey
🙊 speak-no-evil monkey
💋 kiss mark lips
💌 love letter
💘 heart with arrow
💝 heart with ribbon
💖 sparkling heart
💗 growing heart
💓 beating heart
💞 revolving hearts
💕 two hearts
💟 heart decoration
❣ heart exclamation
💔 broken heart
❤ red heart love
🧡 orange heart
💛 yellow heart
💚 green heart
💙 blue heart
💜 purple heart
🤎 brown heart
🖤 black heart
🤍 white heart
💯 hundred points
💢 anger symbol
💥 collision boom
💫 dizzy star
💦 sweat droplets
💨 dashing away
💣 bomb
💬 speech balloon
🗨 left speech bubble
🗯 right anger bubble
💭 thought balloon
💤 zzz sleeping
`,
  people: `
👋 waving hand hello bye
🤚 raised back of hand
🖐 hand with fingers splayed
✋ raised hand stop high five
🖖 vulcan salute
👌 ok hand
✌ victory hand peace
🤞 crossed fingers luck
🤟 love-you gesture
🤘 sign of the horns rock
🤙 call me hand
👈 backhand index pointing left
👉 backhand index pointing right
👆 backhand index pointing up
🖕 middle finger
👇 backhand index pointing down
☝ index pointing up
👍 thumbs up like yes good
👎 thumbs down dislike no bad
✊ raised fist
👊 oncoming fist punch
🤛 left-facing fist
🤜 right-facing fist
👏 clapping hands applause bravo
🙌 raising hands celebration hooray
👐 open hands
🤲 palms up together
🤝 handshake deal agreement
🙏 folded hands pray thanks please
✍ writing hand
💅 nail polish
🤳 selfie
💪 flexed biceps strong muscle
🦾 mechanical arm
🦿 mechanical leg
🦵 leg
🦶 foot
👂 ear
🦻 ear with hearing aid
👃 nose
🧠 brain
🦷 tooth
🦴 bone
👀 eyes look
👁 eye
👅 tongue
👄 mouth lips
👶 baby
🧒 child
👦 boy
👧 girl
🧑 person adult
👱 person blond hair
👨 man
🧔 man bearded beard
👩 woman
🧓 older person
👴 old man
👵 old woman
🙍 person frowning
🙎 person pouting
🙅 person gesturing no
🙆 person gesturing ok
💁 person tipping hand
🙋 person raising hand
🧏 deaf person
🙇 person bowing sorry
🤦 person facepalming
🤷 person shrugging
👮 police officer cop
🕵 detective spy
💂 guard
👷 construction worker
🤴 prince
👸 princess
👳 person wearing turban
👲 person with skullcap
🧕 woman with headscarf
🤵 person in tuxedo
👰 person with veil bride
🤰 pregnant woman
🤱 breast-feeding
👼 baby angel
🎅 santa claus
🤶 mrs claus
🦸 superhero
🦹 supervillain
🧙 mage wizard
🧚 fairy
🧛 vampire
🧜 merperson mermaid
🧝 elf
🧞 genie
🧟 zombie
💆 person getting massage
💇 person getting haircut
🚶 person walking
🧍 person standing
🧎 person kneeling
🏃 person running
💃 woman dancing
🕺 man dancing
🕴 person in suit levitating
👯 people with bunny ears
🧖 person in steamy room sauna
🧗 person climbing
🏇 horse racing
⛷ skier
🏂 snowboarder
🏌 person golfing
🏄 person surfing
🚣 person rowing boat
🏊 person swimming
⛹ person bouncing ball
🏋 person lifting weights gym
🚴 person biking
🚵 person mountain biking
🤸 person cartwheeling
🤼 people wrestling
🤽 person playing water polo
🤾 person playing handball
🤹 person juggling
🧘 person in lotus position yoga
🛀 person taking bath
🛌 person in bed
👭 women holding hands
👫 woman and man holding hands
👬 men holding hands
💏 kiss
💑 couple with heart
👪 family
👣 footprints
🗣 speaking head
👤 bust in silhouette
👥 busts in silhouette
🧥 coat
👚 womans clothes
👕 t-shirt shirt
👖 jeans
👔 necktie
👗 dress
👙 bikini
👘 kimono
🥻 sari
🩱 one-piece swimsuit
🩲 briefs
🩳 shorts
👠 high-heeled shoe
👡 womans sandal
👢 womans boot
👞 mans shoe
👟 running shoe sneaker
🥾 hiking boot
🥿 flat shoe
🧦 socks
🧤 gloves
🧣 scarf
🎩 top hat
🧢 billed cap
👒 womans hat
🎓 graduation cap
⛑ rescue workers helmet
👑 crown
💍 ring
💼 briefcase
👜 handbag
👛 purse
👝 clutch bag
🎒 backpack
👓 glasses
🕶 sunglasses
🥽 goggles
🥼 lab coat
`,
  animals: `
🐵 monkey face
🐒 monkey
🦍 gorilla
🦧 orangutan
🐶 dog face puppy
🐕 dog
🦮 guide dog
🐩 poodle
🐺 wolf
🦊 fox
🦝 raccoon
🐱 cat face kitten
🐈 cat
🦁 lion
🐯 tiger face
🐅 tiger
🐆 leopard
🐴 horse face
🐎 horse
🦄 unicorn
🦓 zebra
🦌 deer
🐮 cow face
🐂 ox
🐃 water buffalo
🐄 cow
🐷 pig face
🐖 pig
🐗 boar
🐽 pig nose
🐏 ram
🐑 ewe sheep
🐐 goat
🐪 camel
🐫 two-hump camel
🦙 llama
🦒 giraffe
🐘 elephant
🦏 rhinoceros
🦛 hippopotamus
🐭 mouse face
🐁 mouse
🐀 rat
🐹 hamster
🐰 rabbit face bunny
🐇 rabbit
🐿 chipmunk
🦔 hedgehog
🦇 bat
🐻 bear
🐨 koala
🐼 panda
🦥 sloth
🦦 otter
🦨 skunk
🦘 kangaroo
🦡 badger
🐾 paw prints
🦃 turkey
🐔 chicken
🐓 rooster
🐣 hatching chick
🐤 baby chick
🐥 front-facing baby chick
🐦 bird
🐧 penguin
🕊 dove peace
🦅 eagle
🦆 duck
🦢 swan
🦉 owl
🦩 flamingo
🦚 peacock
🦜 parrot
🐸 frog
🐊 crocodile
🐢 turtle
🦎 lizard
🐍 snake
🐲 dragon face
🐉 dragon
🦕 sauropod
🦖 t-rex dinosaur
🐳 spouting whale
🐋 whale
🐬 dolphin
🐟 fish
🐠 tropical fish
🐡 blowfish
🦈 shark
🐙 octopus
🐚 spiral shell
🐌 snail
🦋 butterfly
🐛 bug caterpillar
🐜 ant
🐝 honeybee bee
🐞 lady beetle ladybug
🦗 cricket
🕷 spider
🕸 spider web
🦂 scorpion
🦟 mosquito
🦠 microbe virus
💐 bouquet flowers
🌸 cherry blossom
💮 white flower
🏵 rosette
🌹 rose
🥀 wilted flower
🌺 hibiscus
🌻 sunflower
🌼 blossom
🌷 tulip
🌱 seedling
🌲 evergreen tree
🌳 deciduous tree
🌴 palm tree
🌵 cactus
🌾 sheaf of rice
🌿 herb
☘ shamrock
🍀 four leaf clover luck
🍁 maple leaf
🍂 fallen leaf
🍃 leaf fluttering in wind
🍄 mushroom
🌍 globe showing europe-africa earth
🌎 globe showing americas earth
🌏 globe showing asia-australia earth
🌐 globe with meridians
🌑 new moon
🌒 waxing crescent moon
🌓 first quarter moon
🌔 waxing gibbous moon
🌕 full moon
🌖 waning gibbous moon
🌗 last quarter moon
🌘 waning crescent moon
🌙 crescent moon
🌚 new moon face
🌛 first quarter moon face
🌜 last quarter moon face
☀ sun sunny
🌝 full moon face
🌞 sun with face
⭐ star
🌟 glowing star
🌠 shooting star
☁ cloud
⛅ sun behind cloud
⛈ cloud with lightning and rain storm
🌤 sun behind small cloud
🌥 sun behind large cloud
🌦 sun behind rain cloud
🌧 cloud with rain
🌨 cloud with snow
🌩 cloud with lightning
🌪 tornado
🌫 fog
🌬 wind face
🌀 cyclone
🌈 rainbow
☂ umbrella
☔ umbrella with rain drops
⛱ umbrella on ground
⚡ high voltage lightning
❄ snowflake
☃ snowman
⛄ snowman without snow
☄ comet
🔥 fire hot lit
💧 droplet water
🌊 water wave sea
`,
  food: `
🍇 grapes
🍈 melon
🍉 watermelon
🍊 tangerine orange
🍋 lemon
🍌 banana
🍍 pineapple
🥭 mango
🍎 red apple
🍏 green apple
🍐 pear
🍑 peach
🍒 cherries
🍓 strawberry
🥝 kiwi fruit
🍅 tomato
🥥 coconut
🥑 avocado
🍆 eggplant
🥔 potato
🥕 carrot
🌽 ear of corn
🌶 hot pepper chili
🥒 cucumber
🥬 leafy green
🥦 broccoli
🧄 garlic
🧅 onion
🥜 peanuts
🌰 chestnut
🍞 bread
🥐 croissant
🥖 baguette bread
🥨 pretzel
🥯 bagel
🥞 pancakes
🧇 waffle
🧀 cheese wedge
🍖 meat on bone
🍗 poultry leg chicken
🥩 cut of meat steak
🥓 bacon
🍔 hamburger burger
🍟 french fries
🍕 pizza
🌭 hot dog
🥪 sandwich
🌮 taco
🌯 burrito
🥙 stuffed flatbread
🧆 falafel
🥚 egg
🍳 cooking fried egg
🥘 shallow pan of food
🍲 pot of food
🥣 bowl with spoon
🥗 green salad
🍿 popcorn
🧈 butter
🧂 salt
🥫 canned food
🍱 bento box
🍘 rice cracker
🍙 rice ball
🍚 cooked rice
🍛 curry rice
🍜 steaming bowl noodles ramen
🍝 spaghetti pasta
🍠 roasted sweet potato
🍢 oden
🍣 sushi
🍤 fried shrimp
🍥 fish cake with swirl
🥮 moon cake
🍡 dango
🥟 dumpling
🥠 fortune cookie
🥡 takeout box
🦀 crab
🦞 lobster
🦐 shrimp
🦑 squid
🦪 oyster
🍦 soft ice cream
🍧 shaved ice
🍨 ice cream
🍩 doughnut donut
🍪 cookie
🎂 birthday cake
🍰 shortcake cake
🧁 cupcake
🥧 pie
🍫 chocolate bar
🍬 candy sweet
🍭 lollipop
🍮 custard
🍯 honey pot
🍼 baby bottle
🥛 glass of milk
☕ hot beverage coffee tea
🍵 teacup without handle green tea
🍶 sake
🍾 bottle with popping cork champagne
🍷 wine glass
🍸 cocktail glass
🍹 tropical drink
🍺 beer mug
🍻 clinking beer mugs cheers
🥂 clinking glasses toast
🥃 tumbler glass whisky
🥤 cup with straw
🧃 beverage box juice
🧉 mate
🧊 ice
🥢 chopsticks
🍽 fork and knife with plate
🍴 fork and knife
🥄 spoon
🔪 kitchen knife
🏺 amphora
`,
  activities: `
🎃 jack-o-lantern halloween
🎄 christmas tree
🎆 fireworks
🎇 sparkler
🧨 firecracker
✨ sparkles
🎈 balloon
🎉 party popper tada celebration
🎊 confetti ball
🎋 tanabata tree
🎍 pine decoration
🎎 japanese dolls
🎏 carp streamer
🎐 wind chime
🎑 moon viewing ceremony
🧧 red envelope
🎀 ribbon
🎁 wrapped gift present
🎗 reminder ribbon
🎟 admission tickets
🎫 ticket
🎖 military medal
🏆 trophy winner
🏅 sports medal
🥇 first place medal gold
🥈 second place medal silver
🥉 third place medal bronze
⚽ soccer ball football
⚾ baseball
🥎 softball
🏀 basketball
🏐 volleyball
🏈 american football
🏉 rugby football
🎾 tennis
🥏 flying disc frisbee
🎳 bowling
🏏 cricket game
🏑 field hockey
🏒 ice hockey
🥍 lacrosse
🏓 ping pong table tennis
🏸 badminton
🥊 boxing glove
🥋 martial arts uniform
🥅 goal net
⛳ flag in hole golf
⛸ ice skate
🎣 fishing pole
🎽 running shirt
🎿 skis
🛷 sled
🥌 curling stone
🎯 direct hit bullseye
🎱 pool 8 ball
🔮 crystal ball
🧿 nazar amulet
🎮 video game controller
🕹 joystick
🎰 slot machine
🎲 game die dice
🧩 puzzle piece
🧸 teddy bear
♠ spade suit
♥ heart suit
♦ diamond suit
♣ club suit
♟ chess pawn
🃏 joker
🀄 mahjong red dragon
🎴 flower playing cards
🎭 performing arts theatre
🖼 framed picture
🎨 artist palette art
🧵 thread
🧶 yarn
🎼 musical score
🎵 musical note
🎶 musical notes
🎤 microphone karaoke
🎧 headphone
🎷 saxophone
🎸 guitar
🎹 musical keyboard piano
🎺 trumpet
🎻 violin
🥁 drum
🎬 clapper board movie
🏹 bow and arrow
`,
  travel: `
🚗 automobile car
🚕 taxi
🚙 sport utility vehicle suv
🚌 bus
🚎 trolleybus
🏎 racing car
🚓 police car
🚑 ambulance
🚒 fire engine
🚐 minibus
🚚 delivery truck
🚛 articulated lorry
🚜 tractor
🛴 kick scooter
🚲 bicycle bike
🛵 motor scooter
🏍 motorcycle
🚨 police car light
🚔 oncoming police car
🚍 oncoming bus
🚘 oncoming automobile
🚖 oncoming taxi
🚡 aerial tramway
🚠 mountain cableway
🚟 suspension railway
🚃 railway car
🚋 tram car
🚞 mountain railway
🚝 monorail
🚄 high-speed train
🚅 bullet train
🚈 light rail
🚂 locomotive
🚆 train
🚇 metro
🚊 tram
🚉 station
✈ airplane plane flight
🛫 airplane departure
🛬 airplane arrival
🛩 small airplane
💺 seat
🛰 satellite
🚀 rocket
🛸 flying saucer ufo
🚁 helicopter
🛶 canoe
⛵ sailboat
🚤 speedboat
🛥 motor boat
🛳 passenger ship
⛴ ferry
🚢 ship
⚓ anchor
⛽ fuel pump
🚧 construction
🚦 vertical traffic light
🚥 horizontal traffic light
🚏 bus stop
🗺 world map
🗿 moai
🗽 statue of liberty
🗼 tokyo tower
🏰 castle
🏯 japanese castle
🏟 stadium
🎡 ferris wheel
🎢 roller coaster
🎠 carousel horse
⛲ fountain
🏖 beach with umbrella
🏝 desert island
🏜 desert
🌋 volcano
⛰ mountain
🏔 snow-capped mountain
🗻 mount fuji
🏕 camping
⛺ tent
🏠 house home
🏡 house with garden
🏘 houses
🏚 derelict house
🏗 building construction
🏭 factory
🏢 office building
🏬 department store
🏣 japanese post office
🏤 post office
🏥 hospital
🏦 bank
🏨 hotel
🏪 convenience store
🏫 school
🏩 love hotel
💒 wedding
🏛 classical building
⛪ church
🕌 mosque
🕍 synagogue
🕋 kaaba
⛩ shinto shrine
🛤 railway track
🛣 motorway
🗾 map of japan
🏞 national park
🌅 sunrise
🌄 sunrise over mountains
🌆 cityscape at dusk
🌇 sunset
🌉 bridge at night
🌃 night with stars
🏙 cityscape
🌌 milky way
🌁 foggy
♨ hot springs
🧳 luggage
⌛ hourglass done
⏳ hourglass not done
⌚ watch
⏰ alarm clock
⏱ stopwatch
⏲ timer clock
🕰 mantelpiece clock
🕛 twelve oclock
🕐 one oclock
🕑 two oclock
🕒 three oclock
🕓 four oclock
🕔 five oclock
🕕 six oclock
🕖 seven oclock
🕗 eight oclock
🕘 nine oclock
🕙 ten oclock
🕚 eleven oclock
`,
  objects: `
📱 mobile phone
📲 mobile phone with arrow
☎ telephone
📞 telephone receiver
📟 pager
📠 fax machine
🔋 battery
🔌 electric plug
💻 laptop computer
🖥 desktop computer
🖨 printer
⌨ keyboard
🖱 computer mouse
🖲 trackball
💽 computer disk
💾 floppy disk save
💿 optical disk
📀 dvd
🧮 abacus
🎥 movie camera
🎞 film frames
📽 film projector
📺 television tv
📷 camera
📸 camera with flash
📹 video camera
📼 videocassette
🔍 magnifying glass tilted left search
🔎 magnifying glass tilted right search
🕯 candle
💡 light bulb idea
🔦 flashlight
🏮 red paper lantern
📔 notebook with decorative cover
📕 closed book
📖 open book
📗 green book
📘 blue book
📙 orange book
📚 books
📓 notebook
📒 ledger
📃 page with curl
📜 scroll
📄 page facing up document
📰 newspaper
🗞 rolled-up newspaper
📑 bookmark tabs
🔖 bookmark
🏷 label
💰 money bag
💴 yen banknote
💵 dollar banknote
💶 euro banknote
💷 pound banknote
💸 money with wings
💳 credit card
🧾 receipt
💹 chart increasing with yen
✉ envelope mail
📧 e-mail
📨 incoming envelope
📩 envelope with arrow
📤 outbox tray
📥 inbox tray
📦 package parcel
📫 closed mailbox with raised flag
📪 closed mailbox with lowered flag
📬 open mailbox with raised flag
📭 open mailbox with lowered flag
📮 postbox
🗳 ballot box with ballot
✏ pencil
✒ black nib
🖋 fountain pen
🖊 pen
🖌 paintbrush
🖍 crayon
📝 memo note
📁 file folder
📂 open file folder
🗂 card index dividers
📅 calendar
📆 tear-off calendar
🗒 spiral notepad
🗓 spiral calendar
📇 card index
📈 chart increasing
📉 chart decreasing
📊 bar chart
📋 clipboard
📌 pushpin
📍 round pushpin location
📎 paperclip
🖇 linked paperclips
📏 straight ruler
📐 triangular ruler
✂ scissors
🗃 card file box
🗄 file cabinet
🗑 wastebasket trash
🔒 locked
🔓 unlocked
🔏 locked with pen
🔐 locked with key
🔑 key
🗝 old key
🔨 hammer
🪓 axe
⛏ pick
⚒ hammer and pick
🛠 hammer and wrench tools
🗡 dagger
⚔ crossed swords
🔫 pistol water gun
🛡 shield
🔧 wrench
🔩 nut and bolt
⚙ gear settings
🗜 clamp
⚖ balance scale
🦯 white cane
🔗 link
⛓ chains
🧰 toolbox
🧲 magnet
⚗ alembic
🧪 test tube
🧫 petri dish
🧬 dna
🔬 microscope
🔭 telescope
📡 satellite antenna
💉 syringe
🩸 drop of blood
💊 pill
🩹 adhesive bandage
🩺 stethoscope
🚪 door
🛏 bed
🛋 couch and lamp
🪑 chair
🚽 toilet
🚿 shower
🛁 bathtub
🧴 lotion bottle
🧷 safety pin
🧹 broom
🧺 basket
🧻 roll of paper
🧼 soap
🧽 sponge
🧯 fire extinguisher
🛒 shopping cart
🚬 cigarette
⚰ coffin
⚱ funeral urn
`,
  symbols: `
🏧 atm sign
🚮 litter in bin sign
🚰 potable water
♿ wheelchair symbol
🚹 mens room
🚺 womens room
🚻 restroom
🚼 baby symbol
🚾 water closet
🛂 passport control
🛃 customs
🛄 baggage claim
🛅 left luggage
⚠ warning
🚸 children crossing
⛔ no entry
🚫 prohibited
🚳 no bicycles
🚭 no smoking
🚯 no littering
🚱 non-potable water
🚷 no pedestrians
📵 no mobile phones
🔞 no one under eighteen
☢ radioactive
☣ biohazard
⬆ up arrow
↗ up-right arrow
➡ right arrow
↘ down-right arrow
⬇ down arrow
↙ down-left arrow
⬅ left arrow
↖ up-left arrow
↕ up-down arrow
↔ left-right arrow
↩ right arrow curving left
↪ left arrow curving right
⤴ right arrow curving up
⤵ right arrow curving down
🔃 clockwise vertical arrows
🔄 counterclockwise arrows button
🔙 back arrow
🔚 end arrow
🔛 on arrow
🔜 soon arrow
🔝 top arrow
🛐 place of worship
⚛ atom symbol
🕉 om
✡ star of david
☸ wheel of dharma
☯ yin yang
✝ latin cross
☦ orthodox cross
☪ star and crescent
☮ peace symbol
🕎 menorah
🔯 dotted six-pointed star
♈ aries
♉ taurus
♊ gemini
♋ cancer
♌ leo
♍ virgo
♎ libra
♏ scorpio
♐ sagittarius
♑ capricorn
♒ aquarius
♓ pisces
⛎ ophiuchus
🔀 shuffle tracks button
🔁 repeat button
🔂 repeat single button
▶ play button
⏩ fast-forward button
⏭ next track button
⏯ play or pause button
◀ reverse button
⏪ fast reverse button
⏮ last track button
🔼 upwards button
⏫ fast up button
🔽 downwards button
⏬ fast down button
⏸ pause button
⏹ stop button
⏺ record button
⏏ eject button
🎦 cinema
🔅 dim button
🔆 bright button
📶 antenna bars signal
📳 vibration mode
📴 mobile phone off
♀ female sign
♂ male sign
⚕ medical symbol
♾ infinity
♻ recycling symbol
⚜ fleur-de-lis
🔱 trident emblem
📛 name badge
🔰 japanese symbol for beginner
⭕ hollow red circle
✅ check mark button done
☑ check box with check
✔ check mark
✖ multiplication sign
❌ cross mark
❎ cross mark button
➕ plus sign
➖ minus sign
➗ division sign
➰ curly loop
➿ double curly loop
〽 part alternation mark
✳ eight-spoked asterisk
✴ eight-pointed star
❇ sparkle
‼ double exclamation mark
⁉ exclamation question mark
❓ question mark
❔ white question mark
❕ white exclamation mark
❗ exclamation mark
〰 wavy dash
© copyright
® registered
™ trade mark
🔟 keycap 10
🔠 input latin uppercase
🔡 input latin lowercase
🔢 input numbers
🔣 input symbols
🔤 input latin letters
🅰 a button blood type
🆎 ab button blood type
🅱 b button blood type
🆑 cl button
🆒 cool button
🆓 free button
ℹ information
🆔 id button
Ⓜ circled m
🆕 new button
🆖 ng button
🅾 o button blood type
🆗 ok button
🅿 p button parking
🆘 sos button
🆙 up button
🆚 vs button
🔴 red circle
🟠 orange circle
🟡 yellow circle
🟢 green circle
🔵 blue circle
🟣 purple circle
🟤 brown circle
⚫ black circle
⚪ white circle
🟥 red square
🟧 orange square
🟨 yellow square
🟩 green square
🟦 blue square
🟪 purple square
🟫 brown square
⬛ black large square
⬜ white large square
◼ black medium square
◻ white medium square
◾ black medium-small square
◽ white medium-small square
▪ black small square
▫ white small square
🔶 large orange diamond
🔷 large blue diamond
🔸 small orange diamond
🔹 small blue diamond
🔺 red triangle pointed up
🔻 red triangle pointed down
💠 diamond with a dot
🔘 radio button
🔳 white square button
🔲 black square button
`,
};

/* ------------------------------------------------------------------------------------------ */
/* Data preparation                                                                           */
/* ------------------------------------------------------------------------------------------ */

/** @type {RegExp|null} */
let textDefaultRe = null;
/** @type {RegExp|null} */
let emojiRe = null;
try {
  textDefaultRe = new RegExp('^\\p{Emoji_Presentation}', 'u');
  emojiRe = new RegExp('^\\p{Emoji}', 'u');
} catch (_) {
  textDefaultRe = null;
  emojiRe = null;
}

/**
 * Give text-default single code points (the heart, the sun, ...) their emoji presentation selector.
 * @param {string} e
 * @returns {string}
 */
function withPresentation(e) {
  const s = e.replace(/️+/g, '️');
  if (s.indexOf('️') >= 0 || Array.from(s).length !== 1 || !textDefaultRe || !emojiRe) return s;
  return !textDefaultRe.test(s) && emojiRe.test(s) ? `${s}️` : s;
}

/**
 * @param {string} block
 * @returns {Array<{e: string, n: string}>}
 */
function parseBlock(block) {
  const items = [];
  for (const line of block.split('\n')) {
    const t = line.trim();
    if (t === '') continue;
    const sp = t.indexOf(' ');
    items.push({ e: withPresentation(sp < 0 ? t : t.slice(0, sp)), n: sp < 0 ? '' : t.slice(sp + 1).toLowerCase() });
  }
  return items;
}

/** @type {Array<{id: string, label: string, icon: string, items: Array<{e: string, n: string}>}>|null} */
let categories = null;
/** @type {Array<{e: string, n: string}>|null} */
let flat = null;
/** @type {Map<string, string>} emoji -> keywords, for accessible labels */
const names = new Map();

/**
 * Cache key of the canvas support test (user agent hash).
 * @returns {string}
 */
function supportKey() {
  let hash = 0x811c9dc5;
  const ua = typeof navigator !== 'undefined' ? navigator.userAgent || '' : '';
  for (let i = 0; i < ua.length; i += 1) {
    hash ^= ua.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `emoji_ok:${hash.toString(36)}`;
}

/**
 * Emoji the browser/OS cannot draw: its width equals that of an unassigned code point (SPEC 9.8).
 * @param {string[]} list
 * @returns {Set<string>}
 */
function unsupportedEmoji(list) {
  const key = supportKey();
  const cached = storage.getGlobal(key, null);
  if (cached && cached.v === 1 && Array.isArray(cached.bad)) return new Set(cached.bad);
  const bad = new Set();
  try {
    const ctx = document.createElement('canvas').getContext('2d');
    if (!ctx) return bad;
    ctx.font = '32px system-ui, "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif';
    const tofu = ctx.measureText('\u{10FFFE}').width;
    for (const e of list) if (ctx.measureText(e).width === tofu) bad.add(e);
    storage.setGlobal(key, { v: 1, bad: Array.from(bad) });
  } catch (_) {
    bad.clear();
  }
  return bad;
}

/**
 * The emoji categories (canvas-filtered once per user agent, built on first use).
 * @returns {Array<{id: string, label: string, icon: string, items: Array<{e: string, n: string}>}>}
 */
export function getEmojiCategories() {
  if (categories) return categories;
  const raw = CATEGORY_META.map((m) => ({ ...m, items: parseBlock(DATA[m.id] || '') }));
  const bad = unsupportedEmoji(raw.flatMap((c) => c.items.map((i) => i.e)));
  categories = raw.map((c) => ({ ...c, items: c.items.filter((i) => !bad.has(i.e)) })).filter((c) => c.items.length > 0);
  flat = categories.flatMap((c) => c.items);
  for (const i of flat) names.set(i.e, i.n);
  return categories;
}

/**
 * Keyword search: items whose word starts with every query word rank first, then plain substring hits.
 * @param {string} query
 * @param {number} [limit=80]
 * @returns {string[]}
 */
export function searchEmoji(query, limit = 80) {
  const q = fold(String(query || '')).trim();
  if (q === '') return [];
  getEmojiCategories();
  const words = q.split(/\s+/);
  const first = [];
  const second = [];
  for (const item of flat || []) {
    const hay = item.n;
    const hayWords = hay.split(' ');
    if (words.every((w) => hayWords.some((hw) => hw.startsWith(w)))) first.push(item.e);
    else if (words.every((w) => hay.includes(w))) second.push(item.e);
  }
  return first.concat(second).slice(0, limit);
}

/* ------------------------------------------------------------------------------------------ */
/* Recents                                                                                    */
/* ------------------------------------------------------------------------------------------ */

/** @returns {string[]} recently used emoji, newest first */
export function getRecentEmoji() {
  const v = storage.get('emoji_recent', []);
  return Array.isArray(v) ? v.filter((e) => typeof e === 'string').slice(0, MAX_RECENT) : [];
}

/**
 * @param {string} emoji
 */
export function pushRecentEmoji(emoji) {
  if (typeof emoji !== 'string' || emoji === '') return;
  const list = [emoji, ...getRecentEmoji().filter((e) => e !== emoji)].slice(0, MAX_RECENT);
  storage.set('emoji_recent', list);
}

/* ------------------------------------------------------------------------------------------ */
/* Picker component                                                                           */
/* ------------------------------------------------------------------------------------------ */

/**
 * Names of an emoji for its accessible label.
 * @param {string} e
 * @returns {string}
 */
function labelOf(e) {
  return names.get(e) || e;
}

/**
 * Build the emoji picker: category tabs, a search box, a "Recent" section and the emoji grid.
 * @param {{onPick: (emoji: string) => void, search?: boolean}} opts
 * @returns {{el: HTMLElement, focusSearch: () => void, destroy: () => void}}
 */
export function createEmojiPicker(opts) {
  const { onPick, search = true } = opts;
  const cats = getEmojiCategories();
  const recents = getRecentEmoji();
  const sections = [];
  if (recents.length) sections.push({ id: 'recent', label: 'Recent', icon: '\u{1F552}', items: recents.map((e) => ({ e, n: '' })) });
  for (const c of cats) sections.push(c);

  const grid = h('div.emoji-scroll.scroll-y', { tabIndex: 0, role: 'group', 'aria-label': 'Emoji' });
  const results = h('div.emoji-scroll.scroll-y.emoji-results', { hidden: true, role: 'group', 'aria-label': 'Search results' });
  const input = search
    ? h('input.input.emoji-search', { type: 'search', placeholder: 'Search emoji', 'aria-label': 'Search emoji', autocomplete: 'off', dir: 'auto', onInput: () => runSearch() })
    : null;
  const tabs = h('div.emoji-tabs', { role: 'toolbar', 'aria-label': 'Emoji categories' });
  /** @type {Record<string, HTMLElement>} */
  const sectionEls = {};

  /**
   * @param {string[]} list
   * @returns {HTMLElement[]}
   */
  const buttons = (list) => list.map((e) => h('button.emoji-btn', { type: 'button', dataset: { e }, 'aria-label': labelOf(e), title: labelOf(e) }, e));

  for (const s of sections) {
    const el = h('section.emoji-section', { 'aria-label': s.label },
      h('h3.emoji-cat-title', s.label),
      h('div.emoji-grid', buttons(s.items.map((i) => i.e))));
    sectionEls[s.id] = el;
    grid.appendChild(el);
    tabs.appendChild(h('button.emoji-tab', {
      type: 'button',
      'aria-label': s.label,
      title: s.label,
      onClick: () => {
        if (input) input.value = '';
        runSearch();
        grid.scrollTop = el.offsetTop - grid.offsetTop;
      },
    }, s.icon));
  }

  const onClick = (ev) => {
    const btn = /** @type {HTMLElement|null} */ (ev.target instanceof Element ? ev.target.closest('.emoji-btn') : null);
    if (btn && btn.dataset.e) onPick(btn.dataset.e);
  };
  grid.addEventListener('click', onClick);
  results.addEventListener('click', onClick);

  function runSearch() {
    const q = input ? input.value : '';
    const found = q.trim().length > 0 ? searchEmoji(q) : null;
    results.hidden = found === null;
    grid.hidden = found !== null;
    tabs.hidden = found !== null;
    if (found === null) return;
    results.replaceChildren(...(found.length ? buttons(found) : [h('p.emoji-empty.muted', 'No emoji found')]));
  }

  const el = h('div.emoji-picker', input ? h('div.emoji-search-row', input) : null, tabs, grid, results);
  return {
    el,
    focusSearch: () => {
      if (input) input.focus({ preventScroll: true });
    },
    destroy: () => {
      grid.removeEventListener('click', onClick);
      results.removeEventListener('click', onClick);
    },
  };
}

/**
 * Open the picker as a popover (pointer devices) or a bottom sheet (touch / narrow screens).
 * @param {{anchor?: Element|null, x?: number, y?: number, onPick: (emoji: string) => void, onClose?: () => void, keepOpen?: boolean}} opts
 * @returns {{close: () => void}}
 */
export function openEmojiPicker(opts) {
  const { onPick, onClose, keepOpen = false } = opts;
  /** @type {{close: () => void}|null} */
  let host = null;
  const picker = createEmojiPicker({
    onPick: (e) => {
      pushRecentEmoji(e);
      onPick(e);
      if (!keepOpen && host) host.close();
    },
  });
  const done = () => {
    picker.destroy();
    if (onClose) onClose();
  };
  if (ui.prefersSheet()) {
    host = ui.sheet(h('div.emoji-sheet', picker.el), { title: 'Emoji', onClose: done });
  } else {
    host = ui.popover(picker.el, { anchor: opts.anchor, x: opts.x, y: opts.y, placement: 'top-start', label: 'Emoji', className: 'emoji-popover', onClose: done });
  }
  picker.focusSearch();
  return { close: () => host && host.close() };
}
