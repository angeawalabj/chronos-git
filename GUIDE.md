# 📘 Guide d'Installation et d'Utilisation — Chronos-Git

## Prérequis

| Outil | Version minimum | Vérification |
|-------|-----------------|--------------|
| Python | 3.10+ | `python --version` |
| Git | 2.30+ | `git --version` |
| GPG | Optionnel | `gpg --version` |

---

## Installation

### 1. Cloner le dépôt

```bash
git clone https://github.com/yourname/chronos-git.git
cd chronos-git
```

### 2. Créer un environnement virtuel (recommandé)

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Installer les dépendances

```bash
pip install -r requirements.txt

# Ou en mode développement (pour contribuer)
pip install -e ".[dev]"
```

### 4. Configurer votre token GitHub

```bash
python main.py security setup-token
# Collez votre Fine-grained PAT quand demandé
```

> 💡 **Créez un Fine-grained PAT sur GitHub :**
> Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens
> Permissions requises : `Contents: Read & Write`, `Pull requests: Read & Write`

---

## Démarrage Rapide

### Mode GUI (recommandé)

```bash
python main.py
# ou
python main.py gui
```

L'interface s'ouvre avec le tableau de bord. Allez dans **"Planifier"** pour commencer.

### Mode CLI

```bash
# Aide complète
python main.py cli --help

# Planifier un dossier sur 30 jours
python main.py cli plan \
  --folder ./30-days-scripting \
  --repo /chemin/vers/votre/repo \
  --days 30 \
  --branch feat/30-days-challenge \
  --name "Challenge-30-Jours"

# Simuler sans écrire en base (vérifier d'abord)
python main.py cli plan --folder ./30-days --repo ./repo --days 30 --dry-run

# Voir l'état de vos projets
python main.py cli status

# Forcer le rattrapage manuellement
python main.py cli catchup

# Analyser la dérive (fichiers modifiés/nouveaux)
python main.py cli drift --project 1
```

---

## Utilisation avec un fichier YAML (Personnalisation Absolue)

```bash
# Copiez le template
cp plan.yaml.example mon-projet.yaml

# Éditez selon vos besoins
nano mon-projet.yaml

# Lancez avec le fichier config
python main.py cli plan --config mon-projet.yaml

# Simulez d'abord
python main.py cli plan --config mon-projet.yaml --dry-run
```

### Exemple de YAML complet

```yaml
project: challenge-securite-web
repo_path: /home/user/projets/web-security-challenge
source_folder: /home/user/projets/web-security-challenge/scripts
remote: origin
strategy: daily
start_date: "2026-04-01"
days: 45
branch: feat/web-security-45days
merge_into: main
merge_every: friday
recursive: false

overrides:
  - file: "draft_notes.txt"
    action: skip
  - file: "00_introduction.py"
    date: "2026-04-01 09:00:00"
    message: "feat: kick-off web security challenge 🚀"
  - file: "final_capstone.py"
    date: "2026-05-15 23:00:00"
    message: "🏆 feat: web security challenge COMPLETED — 45 days"
```

---

## Démarrage Automatique (PC éteint → rattrapage automatique)

### Windows

1. Modifiez `CHRONOS_PATH` dans `autostart_windows.bat`
2. Appuyez sur `Win + R`, tapez `shell:startup`
3. Copiez `autostart_windows.bat` dans ce dossier

Le script attendra 30 secondes après le démarrage (pour la connexion réseau),
puis exécutera le rattrapage automatiquement.

### Linux (systemd)

```bash
# Copiez le service
cp chronos-git.service ~/.config/systemd/user/

# Éditez avec votre nom d'utilisateur
nano ~/.config/systemd/user/chronos-git.service

# Activez et démarrez
systemctl --user enable chronos-git.service
systemctl --user start chronos-git.service

# Vérifiez les logs
journalctl --user -u chronos-git.service -f
```

---

## Stratégies de Trophées GitHub

Chronos-Git est conçu pour maximiser votre activité GitHub de manière authentique :

### Activité Quotidienne (Streak)
Les commits sont répartis intelligemment pour maintenir des contributions chaque jour.
Le rattrapage automatique comble les jours d'absence avec les VRAIES dates prévues.

### Pull Shark Badge
Configurez un cycle de merge hebdomadaire (vendredi) :
```yaml
merge_every: friday
```
Cela génère une PR chaque semaine → merge = badge Pull Shark progressif.

### Commits Professionnels
Les messages utilisent les préfixes Conventional Commits (`feat:`, `fix:`, `docs:`...)
ce qui donne un historique lisible et professionnel.

### Badge "Verified" (GPG)
```bash
python main.py security gpg-setup
```
Chaque commit signé GPG affiche un badge vert "Verified" sur GitHub.

---

## Structure des Fichiers Générés

```
~/.chronos-git/
├── chronos.db          # Base de données SQLite (file d'attente)
├── .env                # Fallback token (si Keyring indisponible)
└── logs/
    ├── chronos-2026-04-01.log
    ├── chronos-2026-04-02.log
    └── ... (30 jours de rotation)
```

---

## Commandes de Référence

```bash
# ── Planification ─────────────────────────────────
chronos plan --folder DOSSIER --repo REPO --days 30
chronos plan --config plan.yaml
chronos plan --config plan.yaml --dry-run

# ── Suivi ─────────────────────────────────────────
chronos status                    # Tous les projets
chronos status --project 1        # Un projet spécifique

# ── Rattrapage ────────────────────────────────────
chronos catchup                   # Tous les projets
chronos catchup --project 1       # Un projet
chronos catchup --dry-run         # Simulation

# ── Dérive ────────────────────────────────────────
chronos drift --project 1

# ── Sécurité ──────────────────────────────────────
chronos security setup-token      # Configurer le token
chronos security show-token       # Voir le token masqué
chronos security delete-token     # Supprimer le token
chronos security audit            # Audit Bandit

# ── Projets ───────────────────────────────────────
chronos projects                  # Lister tous les projets

# ── Interface ─────────────────────────────────────
chronos gui                       # Lancer la GUI
```

---

## FAQ

**Q : Le rattrapage utilise-t-il la vraie date ou la date d'aujourd'hui ?**
R : La VRAIE date planifiée. Si un commit était prévu le 15 avril et que votre
PC était éteint, le commit apparaîtra le 15 avril dans votre calendrier GitHub.

**Q : Est-ce que ça "triche" avec GitHub ?**
R : Non. Votre code est réel, vos commits sont réels. Chronos-Git automatise
uniquement la *discipline* du push, pas le code. GitHub tolère l'automatisation
tant que le contenu est authentique.

**Q : Que se passe-t-il si un fichier a été modifié entre la planification et le commit ?**
R : Chronos-Git BLOQUE le commit et vous notifie. La vérification SHA-256 garantit
que seul le code prévu est poussé, pas une version accidentellement modifiée.

**Q : Peut-on utiliser Chronos-Git avec des dépôts privés ?**
R : Oui, à condition que votre Fine-grained PAT ait accès à ces dépôts.

**Q : Comment tester sans risquer de pousser vers GitHub ?**
R : Utilisez `--dry-run` : tout est simulé sans exécuter ni écrire en base.
