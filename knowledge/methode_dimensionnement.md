# Méthode de dimensionnement énergétique AIDE-IoT

Source : cahier des charges AIDE-IoT, sections 4.1 à 4.5 et annexe F.

## Bilan énergétique

Le programme est décomposé en phases : réveil et initialisation, mesure
des capteurs, traitement, transmission Wi-Fi / Bluetooth / LoRa,
réception ou écoute réseau, veille ou deep sleep. Pour chaque phase on
détermine le courant, la tension, la durée et le nombre d'exécutions par
cycle. Les courants ne sont additionnés que lorsque les éléments
fonctionnent simultanément.

- Charge par phase : Qi (mAh) = Ii (mA) × ti (h).
- Charge par cycle : somme des charges des phases.
- Consommation journalière : somme des charges de tous les cycles
  exécutés en 24 h, plus les consommations permanentes.
- Énergie journalière : E_jour (Wh/jour) = somme de Vi × Ii × ti.

Si la batterie et la charge fonctionnent à des tensions différentes (par
exemple 3,7 V et 3,3 V), la comparaison se fait en Wh en tenant compte
des rendements de conversion.

## Batterie

C_batterie ≈ Q_jour × N_jours / (DoD × rendement), où N_jours est
l'autonomie souhaitée, DoD la profondeur de décharge autorisée et
rendement l'efficacité de la chaîne d'alimentation.

Une capacité suffisante ne garantit pas que la batterie puisse fournir le
courant de pointe : le courant admissible, le convertisseur, les
protections et les chutes de tension doivent aussi être vérifiés. Le
seuil de protection de la batterie contre la décharge excessive est
distinct de la tension minimale de fonctionnement du dispositif pendant
une transmission.

## Panneau solaire

P_panneau (W) ≥ E_jour (Wh/jour) / [HSP (h/jour) × rendement_global].

PVGIS est la source principale des heures de soleil équivalentes (HSP),
Open-Meteo la source de secours. Le calcul considère le pire cas : hiver,
jours nuageux consécutifs, ombrage, orientation, poussière, températures
élevées. Les pertes ne doivent pas être comptées deux fois.

## Décision

- Votre solution est adaptée : le matériel existant satisfait les besoins
  et les contraintes ; il est conservé.
- Solution à modifier : un ou plusieurs éléments sont insuffisants,
  incompatibles ou nettement surdimensionnés ; seuls ces éléments sont
  modifiés.
- Vérification incomplète : des données critiques manquent ; la
  plateforme les demande au lieu de conclure.

Seuils de marge initiaux proposés : insuffisant sous 1, adapté autour de
1,2 à 2, fortement surdimensionné au-delà d'environ 3. Ce sont des
propositions à valider, pas des normes universelles.

Aucune donnée manquante ne doit être remplacée par zéro ou par une valeur
présentée comme certaine. La plateforme privilégie la solution la plus
compacte et la plus économique qui respecte l'autonomie demandée et
toutes les contraintes, sans compromettre la fiabilité.
