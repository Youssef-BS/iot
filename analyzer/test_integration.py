"""
Parcours complet de l'application, page par page, comme un utilisateur :
inscription, connexion, assistant en 7 étapes, analyse, rapport,
contrôle d'accès et réinitialisation du mot de passe.
"""

import re
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import CustomUser, Project


SUN = {'worst': 2.3, 'annual': 4.96, 'worst_month': 12, 'source': 'PVGIS'}

PASSWORD = 'Ingenieur-IoT-2026'


def mode_fields(prefix, index, modes):
    data = {}
    for number, (name, current, duration, count) in enumerate(modes):
        data.update({
            f'{prefix}_mode_name_{index}_{number}': name,
            f'{prefix}_mode_current_{index}_{number}': current,
            f'{prefix}_mode_duration_{index}_{number}': duration,
            f'{prefix}_mode_count_{index}_{number}': count,
        })
    return data


@override_settings(AI_AGENTS_ENABLED=False)
class FullJourneyTests(TestCase):

    def register(self, email='ingenieur@example.com'):
        response = self.client.post(reverse('register'), {
            'email': email,
            'first_name': 'Mohamed',
            'last_name': 'Dhaouadi',
            'password1': PASSWORD,
            'password2': PASSWORD,
        })
        self.assertRedirects(response, reverse('home'))
        return CustomUser.objects.get(email=email)

    def create_project(self, name='Capteur LoRa'):
        response = self.client.post(reverse('wizard_project'), {
            'name': name,
            'description': 'Station de mesure LoRa',
            'location_city': 'Tunis',
            'location_country': 'Tunisie',
            'latitude': '36.8065',
            'longitude': '10.1815',
            'installation_type': 'outdoor',
        })
        project = Project.objects.get(name=name)
        self.assertRedirects(response, reverse('wizard_mcu', args=[project.id]))
        return project

    def fill_components(self, project):
        mcu = {'mcu_name_0': 'ESP32 + LoRa SX1276', 'mcu_voltage_0': '3.3'}
        mcu.update(mode_fields('mcu', 0, [
            ('Deep sleep', '0.01', '580000', '1'),
            ('Réveil et mesure', '40', '5000', '1'),
            ('Émission Tx', '250', '10000', '1'),
            ('Écoute Rx', '100', '5000', '1'),
        ]))
        response = self.client.post(reverse('wizard_mcu', args=[project.id]), mcu)
        self.assertRedirects(response, reverse('wizard_peripherals', args=[project.id]))

        peripherals = {'sensor_name_0': 'BME688', 'sensor_voltage_0': '3.3'}
        peripherals.update(mode_fields('sensor', 0, [('Mesure', '2.1', '3000', '1')]))
        response = self.client.post(reverse('wizard_peripherals', args=[project.id]), peripherals)
        self.assertRedirects(response, reverse('wizard_actuators', args=[project.id]))

        response = self.client.post(reverse('wizard_actuators', args=[project.id]), {})
        self.assertRedirects(response, reverse('wizard_firmware', args=[project.id]))

        response = self.client.post(reverse('wizard_firmware', args=[project.id]), {
            'input_type': 'code',
            'source_code': 'void setup() { esp_deep_sleep_start(); }',
        })
        self.assertRedirects(response, reverse('wizard_energy', args=[project.id]))

    def fill_energy(self, project, **overrides):
        data = {
            'battery-objective': 'verify',
            'battery-autonomy_days': '3',
            'battery-chosen_technology': '18650',
            'battery-value_scope': 'cell',
            'battery-nominal_voltage_v': '3.7',
            'battery-capacity_mah': '2600',
            'battery-cells_parallel': '2',
            'battery-battery_type': 'auto',
            'battery-rechargeable_only': 'on',
            'solar-objective': 'verify',
            'solar-chosen_technology': 'monocrystalline',
            'solar-panel_power_w': '5',
            'solar-panel_count': '1',
            'solar-placement': 'outdoor_open',
            'solar-azimuth_deg': '180',
            'solar-tilt_deg': '35',
            'solar-panel_type': 'auto',
        }
        data.update(overrides)
        return self.client.post(reverse('wizard_energy', args=[project.id]), data)

    def launch(self, project):
        with patch('analyzer.services.pipeline.get_sun_hours', return_value=SUN):
            return self.client.post(reverse('project_analyze'), {
                'draft_id': str(project.id),
                'use_saved_components': '1',
            })

    # ------------------------------------------------------------------

    def test_public_pages_and_login_protection(self):
        for name in ('home', 'workflow_intro', 'login', 'register', 'forgot_password'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

        response = self.client.get(reverse('wizard_project'))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('wizard_project')}")

        response = self.client.get(reverse('project_list'))
        self.assertEqual(response.status_code, 302)

    def test_complete_user_journey(self):
        # 1. Inscription, déconnexion, reconnexion
        self.register()
        self.client.post(reverse('logout'))
        response = self.client.post(reverse('login'), {
            'username': 'ingenieur@example.com', 'password': PASSWORD,
        })
        self.assertRedirects(response, reverse('home'))

        # 2. Assistant, étapes 1 à 6
        project = self.create_project()
        self.fill_components(project)

        response = self.fill_energy(project)
        self.assertRedirects(response, reverse('wizard_review', args=[project.id]))

        # 3. Vérification (étape 7)
        response = self.client.get(reverse('wizard_review', args=[project.id]))
        self.assertContains(response, 'ESP32 + LoRa SX1276')
        self.assertContains(response, 'BME688')
        self.assertContains(response, 'Lancer l’analyse complète')

        # Les valeurs saisies se rechargent dans les champs numériques
        # (décimale avec un point, sinon le navigateur les efface).
        response = self.client.get(reverse('wizard_mcu', args=[project.id]))
        self.assertContains(response, 'value="0.01"')
        self.assertContains(response, 'value="580000.0"')

        # Toutes les étapes sont accessibles depuis la liste des étapes.
        for step in ('wizard_peripherals', 'wizard_actuators', 'wizard_firmware', 'wizard_energy'):
            self.assertEqual(self.client.get(reverse(step, args=[project.id])).status_code, 200, step)

        # 4. Analyse
        response = self.launch(project)
        self.assertRedirects(response, reverse('report', args=[project.id]))

        project.refresh_from_db()
        self.assertEqual(project.status, 'completed')
        self.assertAlmostEqual(project.result.daily_energy_mah, 3205.8 * 144 / 3600 + 2.1 * 3 * 144 / 3600)

        # 5. Rapport : annexe E, matériel surdimensionné
        response = self.client.get(reverse('report', args=[project.id]))
        self.assertContains(response, 'Solution à modifier')
        self.assertContains(response, 'Alimentation à utiliser')
        self.assertContains(response, 'Comparaison avant / après')
        self.assertContains(response, '<svg')

        # 6. Comparaison avec une mesure réelle (en µA, virgule française)
        response = self.client.post(
            reverse('report_measurement', args=[project.id]),
            {'value': '5450,0', 'unit': 'ua', 'notes': 'PPK2, 3 cycles'},
        )
        self.assertRedirects(
            response, reverse('report', args=[project.id]) + '#mesure',
            fetch_redirect_response=False,
        )
        project.refresh_from_db()
        self.assertAlmostEqual(project.measured_current_ma, 5.45)

        response = self.client.get(reverse('report', args=[project.id]))
        self.assertContains(response, 'La mesure confirme le calcul.')
        self.assertContains(response, 'PPK2, 3 cycles')

        self.client.post(reverse('report_measurement', args=[project.id]), {'action': 'clear'})
        project.refresh_from_db()
        self.assertIsNone(project.measured_current_ma)

        # 7. Liste, accueil, profil
        self.assertContains(self.client.get(reverse('project_list')), 'Terminé')
        self.assertContains(self.client.get(reverse('home')), 'Reprendre là où vous en étiez')
        self.assertContains(self.client.get(reverse('profile')), 'ingenieur@example.com')

        # 7. Un autre utilisateur n'a accès à rien
        self.client.post(reverse('logout'))
        self.register('autre@example.com')
        for name in ('report', 'wizard_mcu', 'wizard_review'):
            self.assertEqual(
                self.client.get(reverse(name, args=[project.id])).status_code, 404, name,
            )

    def test_recommendation_without_solar(self):
        self.register()
        project = self.create_project()
        self.fill_components(project)

        response = self.fill_energy(project, **{
            'battery-objective': 'recommend',
            'battery-capacity_mah': '',
            'battery-nominal_voltage_v': '',
            'solar-objective': 'none',
            'solar-placement': '',
        })
        self.assertRedirects(response, reverse('wizard_review', args=[project.id]))

        self.launch(project)
        response = self.client.get(reverse('report', args=[project.id]))
        self.assertContains(response, 'Solution dimensionnée')
        self.assertContains(response, 'Sans alimentation solaire')

    def test_wizard_validation_errors(self):
        self.register()
        project = self.create_project()

        response = self.client.post(reverse('wizard_mcu', args=[project.id]), {})
        self.assertContains(response, 'Ajoutez au moins un microcontrôleur.')

        response = self.fill_energy(project, **{'battery-capacity_mah': ''})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Indiquez la capacité.')

        response = self.fill_energy(project, **{'solar-placement': ''})
        self.assertContains(response, 'Précisez où le panneau sera installé')

    @override_settings(AI_AGENTS_ENABLED=True, OLLAMA_BASE_URL='http://127.0.0.1:9')
    def test_report_still_complete_when_ollama_is_down(self):
        self.register()
        project = self.create_project()
        self.fill_components(project)
        self.fill_energy(project)

        response = self.launch(project)
        self.assertRedirects(response, reverse('report', args=[project.id]))

        response = self.client.get(reverse('report', args=[project.id]))
        self.assertContains(response, 'Agents IA indisponibles')
        self.assertContains(response, 'Solution à modifier')

    def test_password_reset_with_emailed_code(self):
        self.register()
        self.client.post(reverse('logout'))

        response = self.client.post(reverse('forgot_password'), {'email': 'ingenieur@example.com'})
        self.assertRedirects(response, reverse('verify_reset_code'))
        code = re.search(r'\b(\d{6})\b', mail.outbox[0].body).group(1)

        response = self.client.post(reverse('verify_reset_code'), {'code': code})
        self.assertRedirects(response, reverse('new_password'))

        new_password = 'Nouveau-Mot-2026'
        response = self.client.post(reverse('new_password'), {
            'password1': new_password, 'password2': new_password,
        })
        self.assertRedirects(response, reverse('login'))

        self.assertTrue(self.client.login(username='ingenieur@example.com', password=new_password))
