# Piletivalvur – Draamateater ja Linnateater

Kontrollib iga 5 minuti järel Eesti Draamateatri ja Tallinna Linnateatri mängukava ning uudiseid. Teavituse saadab Macile ja telefoni (ntfy), kui:

| Sündmus | Teavitus |
|---|---|
| Müügi algus on teada (uudisest või Linnateatri mängukavast) | kohe + eelmisel õhtul kl 20 + 15 min enne algust |
| Mängukavva lisandub ≥5 etendust (uus müügiperiood) | üks koondteavitus |
| Lisandub üksik etendus (nt lisaetendus) | etenduse kaupa |
| Müük avaneb (Linnateatris „Müük algab …“ → „Osta pilet“) | koondteavitus või etenduse kaupa |
| Välja müüdud etendusele tekib „Osta pilet“ | „Vabanes pileteid!“ + otselink ostulehele |

| | Draamateater | Linnateater |
|---|---|---|
| Allikas | [mängukava](https://www.draamateater.ee/mangukava/) HTML | [mängukava](https://linnateater.ee/mangukava/) taga olev `shows.json` (sisaldab ka müügi algusaega) |
| Ostuleht | Piletimaailm | Piletilevi |
| Müügis etendused | [`?tickets=available`](https://www.draamateater.ee/mangukava/?tickets=available) | [`?filter=available`](https://linnateater.ee/mangukava/?filter=available) |
| Müük avaneb tavaliselt | E kl 10, kaks kuud korraga | kl 11, kolm kuud korraga |

Teavitusele klõpsates avaneb ostuleht: vabanenud piletite ja üksiku etenduse korral selle etenduse ostuleht, muudel juhtudel teatri müügis etenduste mängukava. Telefonis on lisaks nupp „Osta pilet“ või „Müügis etendused“, uudiste puhul ka „Loe uudist“. Maci teavitused kasutavad `terminal-notifier`-it (`brew install terminal-notifier`), sest ilma selleta ei ava klõps linki.

## Telefon (ntfy)

1. Paigalda **ntfy** rakendus (App Store / Google Play).
2. Vajuta „+“ ja telli teema, mille Claude sulle andis. See on salvestatud GitHubi saladusse `NTFY_TOPIC`.
3. Teavitusele vajutades avaneb ostuleht.

Teema nimi on juhuslik ja toimib nagu parool: kes seda teab, näeb samu teavitusi.

## Failid

Töötav koopia asub kaustas `~/Library/Application Support/Piletivalvur/`, sest macOS ei luba taustaprotsessil Documents kausta lugeda.

- `config.json` – seaded (muudatus hakkab kehtima järgmisel kontrollil)
  - `theaters`: jälgitavad teatrid, vaikimisi `["draamateater", "linnateater"]`
  - `watch_titles`: `[]` = kõik lavastused; nt `["Hamlet", "Mets"]` – vabanenud piletitest ja üksikutest uutest etendustest teavitatakse ainult nende kohta
  - `quiet_hours`: selle aja sees ei kontrollita (vaikimisi 00–07); öised muutused tulevad hommikul
  - `mac_notifications`, `ntfy_topic` – kanalid
- `piletivalvur.log` – kõik teavitused ja vead
- `state.json` – viimati nähtud seis

## Käsud

```bash
/usr/bin/python3 ~/Library/Application\ Support/Piletivalvur/piletivalvur.py --status
```

```bash
/usr/bin/python3 ~/Library/Application\ Support/Piletivalvur/piletivalvur.py --test
```

Pärast `piletivalvur.py` muutmist siin kaustas käivita uuesti `./install.sh`, mis uuendab koopiat. Seaded jäävad alles. Peatamiseks käivita `./install.sh --uninstall`.

## Pilv (GitHub Actions, 24/7)

Sama skript jookseb ka GitHub Actionsis töövoo [`.github/workflows/piletivalvur.yml`](.github/workflows/piletivalvur.yml) järgi, umbes iga 5 minuti järel ööpäev läbi.

- **Telefoniteavitused saadab pilv.** Maci koopia näitab ainult Maci teavitusi, et telefoni ei tuleks iga teadet kaks korda.
- Pilve seaded on failis `config.cloud.json`. ntfy teema on GitHubi saladuses `NTFY_TOPIC` ega satu avalikku reposse.
- Vaikset aega pilves pole. Öösel vaigistab teavitused telefoni enda une- või fookusrežiim.
- Seis (`state.json`) salvestatakse reposse ainult siis, kui midagi muutus.
- Proovitekst: GitHubis vali **Actions → Piletivalvur → Run workflow** ja märgi „Saada ainult proovitekst“.
- GitHub käivitab ajastatud töid vahel 5–15 minuti hilinemisega. Kui Mac on ärkvel, jõuab Maci teavitus tavaliselt varem.
- Muudatused: muuda faile siin kaustas, tee `git pull`, siis commit ja `git push`. Pärast `piletivalvur.py` muutmist käivita ka `./install.sh`, et Maci koopia uueneks.

## Piirangud

- Maci koopia töötab ainult siis, kui Mac on ärkvel. Unest ärgates tehakse kontroll kohe.
- Piletimaailma ja Piletilevi lehti otse ei loeta. Seisu võetakse teatri enda mängukavast, mis võib piletimüüjast mõne minuti maha jääda.
- Vabanenud pilet võib kiiresti uuesti ära minna, seega reageeri teavitusele kohe.
- Skript ei osta pileteid ise, ost jääb sinu teha.
