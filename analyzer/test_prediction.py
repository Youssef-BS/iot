from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from langchain_core.runnables import RunnableLambda

from .models import (
    AnalysisResult,
    BatteryPreference,
    Component,
    ConsumptionMode,
    CustomUser,
    Project,
    SolarPreference,
)
from .services import agents
from .services.energy_model import compute_energy_profile
from .services.pipeline import run_analysis
from .services.sizing import classify_margin, measurement_comparison, size_system


SUN = {
    'worst': 3.0, 'annual': 5.5, 'worst_month': 'décembre',
    'source': 'PVGIS', 'warning': None, 'monthly': [],
    'latitude': 36.8, 'longitude': 10.18,
}


class BaseProject(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email='prediction@example.com',
            password='test-password-123',
        )
        self.client.force_login(self.user)

        self.project = Project.objects.create(
            user=self.user,
            name='Capteur LoRa',
            location_city='Tunis',
            location_country='Tunisie',
            latitude=36.8,
            longitude=10.18,
            installation_type='outdoor',
        )

    def add(self, name, voltage, modes, category='mcu'):
        component = Component.objects.create(
            project=self.project, name=name, category=category, voltage=voltage,
        )
        for mode_name, current, duration in modes:
            ConsumptionMode.objects.create(
                component=component, name=mode_name,
                current_ma=current, duration_ms=duration,
            )
        return component


class EnergyModelTests(BaseProject):

    def test_cahier_des_charges_annexe_d(self):
        """Exemple chiffré de l'annexe D du cahier des charges."""

        self.add('Carte LoRa', 3.3, [
            ('Deep sleep', 0.01, 580_000),
            ('Réveil et mesure', 40, 5_000),
            ('Émission Tx', 250, 10_000),
            ('Écoute Rx', 100, 5_000),
        ])

        profile = compute_energy_profile(self.project)

        self.assertAlmostEqual(profile['cycles_per_day'], 144)
        self.assertAlmostEqual(profile['current_average_ma'], 3205.8 / 600)
        self.assertAlmostEqual(profile['energy_daily_mah'], 3205.8 * 144 / 3600)
        self.assertAlmostEqual(profile['energy_daily_wh'], 0.4232, places=4)
        self.assertEqual(profile['current_max_ma'], 250)
        self.assertEqual(profile['current_min_ma'], 0.01)

    def test_currents_add_only_when_simultaneous(self):
        self.add('ESP32', 3.3, [('Actif', 50, 1000), ('Veille', 0.01, 59000)])
        self.add('BME688', 3.3, [('Mesure', 2, 500), ('Veille', 0.001, 59500)],
                 category='sensor')

        profile = compute_energy_profile(self.project)

        self.assertEqual(profile['cycle_duration_ms'], 60000)
        self.assertAlmostEqual(profile['energy_daily_mah'], 51649.5 * 1440 / 3.6e6)
        self.assertAlmostEqual(profile['current_max_ma'], 52)
        self.assertAlmostEqual(profile['current_min_ma'], 0.011)
        self.assertEqual(
            [round(p['current_ma'], 3) for p in profile['plateaus']],
            [52, 50.001, 0.011],
        )

    def test_cycles_per_day_is_not_truncated(self):
        self.add('MCU', 3.3, [('Actif', 10, 1000), ('Veille', 0.01, 7 * 3_600_000)])
        profile = compute_energy_profile(self.project)
        self.assertAlmostEqual(
            profile['cycles_per_day'], 86_400_000 / (7 * 3_600_000 + 1000),
        )

    def test_missing_voltage_is_reported_as_hypothesis(self):
        self.add('Capteur', None, [('Mesure', 1, 1000)])
        profile = compute_energy_profile(self.project)
        self.assertEqual(profile['voltage_assumed_for'], ['Capteur'])

    def test_no_modes_returns_none(self):
        self.assertIsNone(compute_energy_profile(self.project))


class SizingTests(BaseProject):

    def setUp(self):
        super().setUp()
        self.add('Carte LoRa', 3.3, [
            ('Deep sleep', 0.01, 580_000),
            ('Réveil et mesure', 40, 5_000),
            ('Émission Tx', 250, 10_000),
            ('Écoute Rx', 100, 5_000),
        ])
        self.profile = compute_energy_profile(self.project)

    def verify(self, capacity_mah, panel_w, technology='18650'):
        BatteryPreference.objects.create(
            project=self.project, objective='verify',
            chosen_technology=technology, nominal_voltage_v=3.7,
            capacity_mah=capacity_mah, autonomy_days=3,
        )
        SolarPreference.objects.create(
            project=self.project, objective='verify',
            panel_power_w=panel_w, panel_count=1,
        )
        self.project.refresh_from_db()
        return size_system(self.project, self.profile, SUN)

    def test_margin_thresholds(self):
        self.assertEqual(classify_margin(0.8), 'insufficient')
        self.assertEqual(classify_margin(1.1), 'tight')
        self.assertEqual(classify_margin(1.5), 'adapted')
        self.assertEqual(classify_margin(3.5), 'oversized')
        self.assertEqual(classify_margin(None), 'incomplete')

    def test_existing_solution_is_kept_when_adapted(self):
        # Besoin : 3 jours × 0,423 Wh ; 600 mAh × 3,7 V × 0,8 × 0,9 ≈ 1,6 Wh
        sizing = self.verify(capacity_mah=600, panel_w=0.5)

        self.assertEqual(sizing['battery']['status'], 'adapted')
        self.assertEqual(sizing['solar']['status'], 'adapted')
        self.assertEqual(sizing['decision']['code'], 'adapted')
        self.assertTrue(sizing['hardware']['battery']['keep'])
        self.assertEqual(sizing['comparison'], [])

    def test_oversized_hardware_must_be_modified(self):
        """Annexe E : panneau 5 W et grosse batterie sur un capteur LoRa."""

        sizing = self.verify(capacity_mah=6000, panel_w=5)

        self.assertEqual(sizing['battery']['status'], 'oversized')
        self.assertEqual(sizing['solar']['status'], 'oversized')
        self.assertEqual(sizing['decision']['code'], 'modify')
        self.assertLess(
            sizing['hardware']['solar']['power_w'], 5,
        )
        self.assertTrue(sizing['comparison'])

    def test_non_rechargeable_battery_is_incompatible_with_solar(self):
        sizing = self.verify(capacity_mah=600, panel_w=0.5, technology='alkaline')

        self.assertEqual(sizing['battery']['status'], 'incompatible')
        self.assertEqual(sizing['decision']['code'], 'modify')
        self.assertTrue(sizing['hardware']['battery']['rechargeable'])

    def test_missing_battery_data_gives_incomplete(self):
        BatteryPreference.objects.create(
            project=self.project, objective='verify', autonomy_days=3,
        )
        self.project.refresh_from_db()

        sizing = size_system(self.project, self.profile, SUN)

        self.assertEqual(sizing['decision']['code'], 'incomplete')
        self.assertTrue(sizing['decision']['missing'])

    def test_no_modes_gives_incomplete(self):
        sizing = size_system(self.project, None, SUN)
        self.assertEqual(sizing['decision']['code'], 'incomplete')

    def test_slow_recharge_after_cloudy_days_is_insufficient(self):
        # 0,25 W : marge en puissance 1,33, mais 0,25 × 3 × 0,75 − 0,4232
        # = 0,139 Wh/j d'excédent → ≈ 9,1 jours pour recharger 3 jours.
        sizing = self.verify(capacity_mah=600, panel_w=0.25)

        self.assertEqual(sizing['solar']['status'], 'insufficient')
        self.assertIn('Recharge trop lente', sizing['solar']['reasons'][0])
        self.assertTrue(sizing['solar']['checks'][0]['ok'])  # panneau recommandé

    def test_recommendation_uses_commercial_panel_size(self):
        sizing = size_system(self.project, self.profile, SUN)

        panel = sizing['hardware']['solar']
        self.assertEqual(sizing['decision']['code'], 'proposed')
        self.assertGreaterEqual(panel['power_w'], panel['computed_w'])


class ForestLoRaNodeTests(BaseProject):
    """
    Fiche de soutenance « Nœud IoT LoRa (LoRa-E5 + BME688) » : valeurs
    saisies telles quelles, résultats recalculés à la main.
    """

    def setUp(self):
        super().setUp()
        self.add('LoRa-E5 Dev Board', 3.3, [
            ('Sommeil (Sleep)', 0.0021, 595_000),
            ('Actif (MCU + Rx)', 6.7, 4_000),
            ('Émission (Tx)', 50, 1_000),
        ])
        self.add('BME688', 3.3, [('Gas scan standard', 3.9, 4_000)], category='sensor')
        divider = self.add(
            'Diviseur de tension (PB3)', 3.7,
            [('Permanent (fuite continue)', 0.0123, 600_000)], category='actuator',
        )
        divider.separate_power = True
        divider.save()

        BatteryPreference.objects.create(
            project=self.project, objective='verify', chosen_technology='18650',
            value_scope='cell', nominal_voltage_v=3.7, capacity_mah=3270,
            cells_parallel=2, autonomy_days=3,
        )
        SolarPreference.objects.create(
            project=self.project, objective='verify', panel_power_w=4, panel_count=1,
            placement='outdoor_partial', light_transmittance_percent=3,
        )
        self.project.refresh_from_db()
        self.profile = compute_energy_profile(self.project)
        self.sun = {**SUN, 'worst': 2.5, 'annual': 5.67}

    def test_energy_balance_matches_hand_calculation(self):
        charge_per_cycle = 0.0021 * 595 + 6.7 * 4 + 50 * 1 + 3.9 * 4 + 0.0123 * 600

        self.assertAlmostEqual(charge_per_cycle, 101.0295)
        self.assertAlmostEqual(self.profile['current_average_ma'], 101.0295 / 600)
        self.assertAlmostEqual(self.profile['energy_daily_mah'], 101.0295 * 144 / 3600)
        self.assertAlmostEqual(
            self.profile['energy_daily_wh'],
            ((78.0495 + 15.6) * 3.3 + 7.38 * 3.7) / 3600 * 144 / 1000,
        )
        self.assertAlmostEqual(self.profile['current_max_ma'], 50.0123)

    def test_sizing_with_canopy_shading(self):
        sizing = size_system(self.project, self.profile, self.sun)
        daily = self.profile['energy_daily_wh']

        # Batterie : 2 × 3270 mAh = 17,42 Wh utiles pour 0,04 Wh demandés.
        battery = sizing['battery']
        self.assertAlmostEqual(battery['margin'], 6540 * 3.7 / 1000 * 0.72 / (3 * daily))
        self.assertEqual(battery['status'], 'oversized')
        self.assertEqual(battery['recommended']['capacity_mah'], 1500)

        # Panneau : 2,5 h × 3 % de lumière × 75 % de rendement.
        solar = sizing['solar']
        self.assertAlmostEqual(solar['margin'], 4 / (daily / (2.5 * 0.03 * 0.75)))
        self.assertEqual(solar['status'], 'oversized')
        self.assertEqual(solar['recommended']['power_w'], 0.5)
        self.assertTrue(solar['checks'][0]['ok'])

        self.assertEqual(sizing['decision']['code'], 'modify')
        self.assertTrue(any('MOSFET' in action for action in sizing['actions']))

    def test_fiche_reference_value_confirms_calculation(self):
        """174,9 µA (fiche) face à 168,38 µA calculés : écart de +3,9 %."""

        sizing = size_system(self.project, self.profile, self.sun)
        result = measurement_comparison(
            self.profile, sizing['battery'], sizing['solar'], [], 0.1749,
        )

        self.assertAlmostEqual(result['gap_percent'], (0.1749 / (101.0295 / 600) - 1) * 100)
        self.assertEqual(result['level'], 'agree')
        self.assertTrue(result['battery']['ok'])

    def test_large_measured_gap_flags_missing_consumer(self):
        sizing = size_system(self.project, self.profile, self.sun)
        result = measurement_comparison(
            self.profile, sizing['battery'], sizing['solar'], [], 0.5,
        )

        self.assertEqual(result['level'], 'far')
        self.assertIn('plus que prévu', result['verdict'])

    def test_fiche_solar_margin_uses_annual_sun(self):
        """Le ×37 de la fiche correspond à l'ensoleillement moyen annuel."""

        daily = self.profile['energy_daily_wh']
        self.assertAlmostEqual(4 * 5.67 * 0.03 * 0.75 / daily, 37.9, delta=0.5)


class NumberGuardTests(TestCase):

    def test_invented_numbers_are_removed(self):
        guard = agents.NumberGuard('Énergie journalière : 0,4232 Wh/jour')

        text = guard.clean_text(
            'Le dispositif consomme 0,4232 Wh par jour. '
            'Une batterie de 2500 mAh conviendrait. '
            'La phase Tx domine [S1].'
        )

        self.assertIn('0,4232', text)
        self.assertNotIn('2500', text)
        self.assertIn('[S1]', text)
        self.assertEqual(guard.removed, 1)

    def test_markdown_is_removed(self):
        guard = agents.NumberGuard('Part : 77,8 %')
        text = guard.clean_text('L’**Émission Tx** représente **77,8 %** (`LoRa.endPacket()`).')
        self.assertEqual(text, 'L’Émission Tx représente 77,8 % (LoRa.endPacket()).')

    def test_rounded_and_converted_values_are_accepted(self):
        guard = agents.NumberGuard('Énergie : 0,4232 Wh ; courant 5,343 mA')
        self.assertTrue(guard.is_clean('environ 0,42 Wh soit 423,2 mWh'))
        self.assertTrue(guard.is_clean('courant moyen 5,34 mA'))


@override_settings(AI_AGENTS_ENABLED=True)
class AgentTests(BaseProject):

    def test_agent_output_is_cleaned_and_sources_filtered(self):
        def fake_answer(_prompt_value):
            return agents.ConsumptionAnalysis(
                phases=[agents.Phase(nom='Émission Tx', role='Transmission LoRa.')],
                explication_bilan=(
                    'La charge journalière est 128,232 mAh. '
                    'Le pic atteint 900 mA.'
                ),
                phase_dominante='La transmission domine [S1].',
                observations_programme=[],
                points_attention=['Prévoir 7 jours de marge 4200 mAh.'],
                donnees_manquantes=[],
                sources=['S1', 'S9'],
            )

        fake_llm = SimpleNamespace(
            with_structured_output=lambda *a, **k: RunnableLambda(fake_answer),
        )

        with patch.object(agents, '_llm', return_value=fake_llm):
            result = agents.run_consumption_analyst(
                '- Charge journalière : 128,232 mAh/jour',
                [{'id': 'S1', 'source': 'lora.pdf', 'page': 3, 'text': 'TX 120 mA'}],
            )

        self.assertIn('128,232', result['explication_bilan'])
        self.assertNotIn('900', result['explication_bilan'])
        self.assertEqual(result['points_attention'], [])
        self.assertEqual(result['sources'], ['S1'])
        self.assertEqual(result['removed_sentences'], 2)


@override_settings(AI_AGENTS_ENABLED=False)
class DashboardTests(BaseProject):

    def analyze(self):
        with patch('analyzer.services.pipeline.get_sun_hours', return_value={
            'worst': 3.0, 'annual': 5.5, 'worst_month': 12, 'source': 'PVGIS',
        }):
            report = run_analysis(self.project)

        AnalysisResult.objects.create(
            project=self.project, status='completed',
            analysis_source='python', full_report=report,
        )
        return self.client.get(reverse('report', args=[self.project.id]))

    def test_dashboard_follows_specification_order(self):
        self.add('Carte LoRa', 3.3, [
            ('Deep sleep', 0.01, 580_000),
            ('Émission Tx', 250, 10_000),
        ])

        response = self.analyze()
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        positions = [
            html.index(marker) for marker in (
                'Solution dimensionnée',
                'Alimentation à utiliser',
                'Pourquoi ce résultat',
                'Preuves, sources, hypothèses et limites',
            )
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('<svg', html)
        self.assertIn('PVGIS', html)
        self.assertNotIn('<canvas', html)

    def test_firmware_only_project_is_incomplete(self):
        response = self.analyze()
        self.assertContains(response, 'Vérification incomplète')
        self.assertContains(response, "l&#x27;IA n&#x27;estime pas les courants")

    def test_dashboard_without_result(self):
        response = self.client.get(reverse('report', args=[self.project.id]))
        self.assertContains(response, 'Aucun résultat pour ce projet')

    def test_project_list_renders(self):
        response = self.client.get(reverse('project_list'))
        self.assertContains(response, 'Capteur LoRa')
