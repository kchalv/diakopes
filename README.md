# Οικονομικές οικογενειακές διακοπές

Κάθε ώρα, στους servers του GitHub (GitHub Actions), σαρώνονται τιμές για 2 ενήλικες + 3 παιδιά
(9, 11, 13). Μια τιμή κρίνεται ως πραγματική προσφορά με βάση το ιστορικό. Για τις εξαιρετικές
στέλνεται email και η σελίδα ανανεώνεται στο **https://kchalv.github.io/diakopes/** (αποθετήριο: https://github.com/kchalv/diakopes).
Ο υπολογιστής δεν χρειάζεται να είναι ανοιχτός.

## Πηγές (μόνο ό,τι κλείνεται από Ελλάδα)
| Πηγή | Τι δίνει | Τρόπος |
|---|---|---|
| Ryanair | Ναύλοι από ATH/SKG προς όλους τους προορισμούς | δημόσιο API |
| Google Flights | Όλες οι αεροπορικές (Aegean, Sky Express, Wizz…) με τιμή για 5 | browser (Playwright) |
| Airbnb | Σπίτια/διαμερίσματα για 5, συνολική τιμή | browser |
| Google Hotels | Ξενοδοχεία με ηλικίες παιδιών (συγκεντρώνει Booking, Expedia κ.ά.) | browser |
| Fly4free | Μόνο αναχωρήσεις από Ελλάδα ή σκέτη διαμονή στην Ελλάδα | RSS |

Αφαιρέθηκαν HolidayPirates, TravelPirates, Travelzoo και Urlaubspiraten, γιατί πουλάνε πακέτα με
πτήσεις από Βρετανία/Γερμανία για πελάτες εκείνων των χωρών. Το Booking.com και το Skyscanner
μπλοκάρουν τους αυτοματοποιημένους browsers και εμφανίζονται ως έτοιμοι σύνδεσμοι.

## Πώς λειτουργεί online
- `.github/workflows/scan.yml`: τρέχει κάθε ώρα (λεπτό :17). Το GitHub μπορεί να το καθυστερήσει λίγα λεπτά.
  Χειροκίνητη εκτέλεση: καρτέλα **Actions → Σάρωση προσφορών → Run workflow**.
- Το ιστορικό τιμών (`prices.db`) φυλάγεται στον κλάδο `data` και κρατιέται μόνο η τελευταία έκδοση.
- Τα στοιχεία email (διευθύνσεις, server, κωδικός) είναι στο κρυφό **Secret `DIAKOPES_EMAIL`**
  (Settings → Secrets and variables → Actions) και δεν εμφανίζονται πουθενά στον κώδικα.
  Μορφή: `{"to":"…","from":"…","smtp_host":"…","smtp_user":"…","smtp_password":"…"}`.
  Αν αλλάξετε τον κωδικό του email, ενημερώστε αυτό το Secret.
- Το αποθετήριο είναι δημόσιο, ώστε τα Actions και το Pages να είναι δωρεάν.

## Τοπικά (προαιρετικά)
```powershell
pip install -r requirements.txt; python -m playwright install chromium
python collector.py --no-scrape   # γρήγορα, μόνο Ryanair + RSS
python notify.py --test-email     # δοκιμή email (στοιχεία στο secrets.json, που δεν ανεβαίνει)
start site\index.html
```

## Αρχεία
- `config.json`: οικογένεια, αεροδρόμια, προορισμοί, κανόνες «προσφοράς».
- `collector.py`: κύρια σάρωση. `scrapers.py`: browser scraping. `notify.py`: email.
- `site/index.html`: η σελίδα. Το `data.js` παράγεται σε κάθε σάρωση.
