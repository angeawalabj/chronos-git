# 🔒 Chronos-Git — Politique de Sécurité

## Modèle de Menaces

Chronos-Git gère des tokens GitHub et exécute des opérations Git automatisées.
Le modèle de sécurité adresse deux surfaces d'attaque :

### Surface 1 : L'Utilisateur vs Ses Propres Secrets

**Risque** : Token GitHub stocké en clair → accès à tous les dépôts si le PC est compromis.

**Mitigations** :
- ✅ Stockage dans le gestionnaire de secrets OS (Windows Credential Manager / Keychain / Secret Service)
- ✅ Fine-grained PAT : accès limité aux repos spécifiques, pas aux paramètres compte
- ✅ Fallback `.env` dans `~/.chronos-git/` (hors de tout dépôt Git) avec `chmod 600`
- ✅ Jamais de token dans les logs (le `KeyringManager` ne retourne jamais le token brut à l'UI)

### Surface 2 : L'Automatisation vs L'Intégrité du Code

**Risque** : Un fichier modifié (malware, accident) est poussé automatiquement sans que l'utilisateur le sache.

**Mitigations** :
- ✅ Hash SHA-256 calculé au moment de la PLANIFICATION
- ✅ Hash REVÉRIFIÉ juste avant chaque commit → blocage si différent
- ✅ Kill Switch d'urgence (bouton rouge dans la GUI)
- ✅ Drift Detection : notifie si des fichiers locaux ont changé
- ✅ Validation de chemin (`pathlib.resolve()`) → protection contre path traversal

---

## Checklist de Mise en Place Sécurisée

### Étape 1 : Créer un Fine-Grained PAT GitHub

1. Allez sur **GitHub → Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens**
2. Cliquez **Generate new token**
3. Configurez :
   - **Expiration** : 90 jours (renouvelez régulièrement)
   - **Repository access** : Sélectionnez UNIQUEMENT les repos de vos challenges
   - **Permissions** :
     - `Contents` : Read & Write
     - `Metadata` : Read-only
     - `Pull requests` : Read & Write (pour la fonctionnalité PR)
4. Copiez le token (commence par `github_pat_`)

### Étape 2 : Stocker le Token dans Chronos-Git

```bash
python main.py security setup-token
# Collez votre token quand demandé (invisible à la frappe)
```

### Étape 3 : Configurer la Signature GPG (Optionnel mais recommandé)

Les commits GPG-signés affichent le badge **"Verified"** sur GitHub.

```bash
# Générer une clé GPG
gpg --gen-key

# Obtenir l'ID de la clé
gpg --list-secret-keys --keyid-format=long

# Exporter vers GitHub
gpg --armor --export VOTRE_ID_CLE

# Configurer Git
git config --global user.signingkey VOTRE_ID_CLE
git config --global commit.gpgsign true
```

### Étape 4 : Audit de Sécurité du Code

```bash
# Analyse des vulnérabilités dans le code Python
python main.py security audit

# Ou directement avec Bandit
bandit -r chronos/ -ll

# Audit des secrets accidentels dans l'historique Git
pip install gitleaks
gitleaks detect --source . --verbose
```

---

## Architecture de Sécurité

```
┌─────────────────────────────────────────────────────┐
│                   Chronos-Git                        │
│                                                      │
│  ┌──────────────┐    ┌──────────────────────────┐   │
│  │   KeyringMgr │    │      GitExecutor          │   │
│  │              │    │                          │   │
│  │  OS Keyring  │◄───│  1. Check Kill Switch    │   │
│  │  (crypté)    │    │  2. Verify SHA-256 Hash  │   │
│  │              │    │  3. Get Token (Keyring)  │   │
│  └──────────────┘    │  4. Commit + Push        │   │
│                      │  5. Log to SQLite        │   │
│  ┌──────────────┐    └──────────────────────────┘   │
│  │  SQLite DB   │                                    │
│  │  (local)     │    ┌──────────────────────────┐   │
│  │              │    │      CatchupEngine        │   │
│  │  task_queue  │◄───│  At startup:             │   │
│  │  projects    │    │  - Find overdue tasks     │   │
│  │  exec_logs   │    │  - Execute chronologically│   │
│  └──────────────┘    │  - Use historical dates  │   │
│                      └──────────────────────────┘   │
└─────────────────────────────────────────────────────┘
                          │
                          ▼
                   GitHub API (HTTPS)
                   Token via Keyring
                   (jamais en clair)
```

---

## Ce que Chronos-Git NE FAIT PAS

- ❌ Ne stocke jamais de token en clair dans le code source
- ❌ Ne pousse jamais un fichier dont le hash a changé sans confirmation
- ❌ N'exécute jamais de commandes shell avec des paramètres non-validés
- ❌ N'accède jamais à des chemins hors du dossier source configuré
- ❌ Ne log jamais le contenu des tokens (uniquement les previews masquées)
