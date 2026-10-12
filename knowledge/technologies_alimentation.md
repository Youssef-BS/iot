# Technologies d'alimentation des systèmes IoT

Source : cahier des charges AIDE-IoT, annexe C. Les rendements sont des
ordres de grandeur indicatifs, pas des caractéristiques garanties : les
calculs finaux utilisent la fiche technique du matériel retenu.

## Panneaux solaires

- Monocristallin : rendement souvent annoncé autour de 18 à 22 %. Bon
  rendement surfacique, intéressant lorsque la place est limitée.
- Polycristallin : rendement souvent annoncé autour de 15 à 17 %. Comparer
  coût, disponibilité, surface et puissance réelle.
- Amorphe / couche mince : rendement souvent annoncé autour de 6 à 10 %.
  Demande davantage de surface ; le comportement en lumière diffuse dépend
  du produit.

Le watt-crête (Wc) est la puissance nominale du panneau dans des
conditions standard de test, rarement reproduites exactement en extérieur.

## Technologies de stockage

- Li-ion / LiPo : bonne densité énergétique et format compact. À vérifier :
  plage de température, courant de charge et de décharge, protection et
  durée de vie.
- LiFePO4 : bonne durée de vie cyclique et stabilité thermique
  généralement favorable. Tension nominale et seuils différents du Li-ion ;
  il faut un chargeur compatible LiFePO4.
- Supercondensateur : très forte puissance et grand nombre de cycles, mais
  faible énergie stockée et autodécharge relativement élevée.
- Piles non rechargeables : simples pour certains dispositifs autonomes,
  mais elles ne peuvent pas être rechargées par un panneau solaire ; elles
  ne conviennent donc pas à une architecture de recharge solaire.

La capacité en mAh ne suffit pas à comparer des batteries fonctionnant à
des tensions différentes. Il faut privilégier l'énergie utilisable en Wh,
vérifier le montage série/parallèle, le courant de pointe admissible et la
compatibilité avec les convertisseurs.

## Chargeur, régulateur et conversion

Le chargeur ou régulateur doit être compatible avec la chimie et le
montage de la batterie. Un contrôleur MPPT peut améliorer l'exploitation
du panneau dans certaines configurations par rapport à un régulateur PWM,
mais le choix doit être justifié par les tensions, la puissance, les
conditions d'utilisation et le coût.

Le courant de repos du convertisseur doit être intégré au bilan : il peut
devenir important lorsque la carte est en deep sleep.

Protections à examiner : surcharge, décharge profonde, surintensités,
court-circuit, inversion de polarité et température.
