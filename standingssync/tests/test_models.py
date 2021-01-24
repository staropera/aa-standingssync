from enum import Enum
import copy
import datetime as dt
from unittest.mock import patch, Mock

from django.utils.timezone import now

from esi.models import Token
from esi.errors import TokenExpiredError, TokenInvalidError

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from allianceauth.tests.auth_utils import AuthUtils


from . import (
    LoadTestDataMixin,
    create_test_user,
    ALLIANCE_CONTACTS,
    BravadoOperationStub,
)
from ..models import (
    SyncManager,
    EveContact,
    SyncedCharacter,
    EveEntity,
    EveWar,
)
from ..utils import NoSocketsTestCase


MODELS_PATH = "standingssync.models"
MANAGERS_PATH = "standingssync.managers"


class TestGetEffectiveStanding(LoadTestDataMixin, NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # 1 user with 1 alt character
        cls.user_1 = create_test_user(cls.character_1)
        cls.main_ownership_1 = CharacterOwnership.objects.get(
            character=cls.character_1, user=cls.user_1
        )

        cls.sync_manager = SyncManager.objects.create(
            alliance=cls.alliance_1, character_ownership=cls.main_ownership_1
        )
        contacts = [
            {"contact_id": 1001, "contact_type": "character", "standing": -10},
            {"contact_id": 2001, "contact_type": "corporation", "standing": 10},
            {"contact_id": 3001, "contact_type": "alliance", "standing": 5},
        ]
        for contact in contacts:
            EveContact.objects.create(
                manager=cls.sync_manager,
                eve_entity=EveEntity.objects.get(id=contact["contact_id"]),
                standing=contact["standing"],
                is_war_target=False,
            )

    def test_char_with_character_standing(self):
        c1 = EveCharacter(
            character_id=1001,
            character_name="Char 1",
            corporation_id=201,
            corporation_name="Corporation 1",
            corporation_ticker="C1",
        )
        self.assertEqual(self.sync_manager.get_effective_standing(c1), -10)

    def test_char_with_corporation_standing(self):
        c2 = EveCharacter(
            character_id=1002,
            character_name="Char 2",
            corporation_id=2001,
            corporation_name="Corporation 1",
            corporation_ticker="C1",
        )
        self.assertEqual(self.sync_manager.get_effective_standing(c2), 10)

    def test_char_with_alliance_standing(self):
        c3 = EveCharacter(
            character_id=1003,
            character_name="Char 3",
            corporation_id=2003,
            corporation_name="Corporation 3",
            corporation_ticker="C2",
            alliance_id=3001,
            alliance_name="Alliance 1",
            alliance_ticker="A1",
        )
        self.assertEqual(self.sync_manager.get_effective_standing(c3), 5)

    def test_char_without_standing_and_has_alliance(self):
        c4 = EveCharacter(
            character_id=1003,
            character_name="Char 3",
            corporation_id=2003,
            corporation_name="Corporation 3",
            corporation_ticker="C2",
            alliance_id=3002,
            alliance_name="Alliance 2",
            alliance_ticker="A2",
        )
        self.assertEqual(self.sync_manager.get_effective_standing(c4), 0.0)

    def test_char_without_standing_and_without_alliance_1(self):
        c4 = EveCharacter(
            character_id=1003,
            character_name="Char 3",
            corporation_id=2003,
            corporation_name="Corporation 3",
            corporation_ticker="C2",
            alliance_id=None,
            alliance_name=None,
            alliance_ticker=None,
        )
        self.assertEqual(self.sync_manager.get_effective_standing(c4), 0.0)

    def test_char_without_standing_and_without_alliance_2(self):
        c4 = EveCharacter(
            character_id=1003,
            character_name="Char 3",
            corporation_id=2003,
            corporation_name="Corporation 3",
            corporation_ticker="C2",
        )
        self.assertEqual(self.sync_manager.get_effective_standing(c4), 0.0)


class TestSyncManager(LoadTestDataMixin, NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # create environment
        # 1 user has permission for manager sync
        cls.user_1 = create_test_user(cls.character_1)
        cls.main_ownership_1 = CharacterOwnership.objects.get(
            character=cls.character_1, user=cls.user_1
        )
        cls.user_1 = AuthUtils.add_permission_to_user_by_name(
            "standingssync.add_syncmanager", cls.user_1
        )

        # user 1 has no permission for manager sync and has 1 alt
        cls.user_2 = create_test_user(cls.character_2)
        cls.main_ownership_2 = CharacterOwnership.objects.get(
            character=cls.character_2, user=cls.user_2
        )
        cls.alt_ownership = CharacterOwnership.objects.create(
            character=cls.character_4, owner_hash="x4", user=cls.user_2
        )

    def test_set_sync_status(self):
        sync_manager = SyncManager.objects.create(
            alliance=self.alliance_1, character_ownership=self.main_ownership_1
        )
        sync_manager.last_error = SyncManager.Error.NONE
        sync_manager.last_sync = None

        sync_manager.set_sync_status(SyncManager.Error.TOKEN_INVALID)
        sync_manager.refresh_from_db()

        self.assertEqual(sync_manager.last_error, SyncManager.Error.TOKEN_INVALID)
        self.assertIsNotNone(sync_manager.last_sync)

    def test_should_abort_when_no_char(self):
        # given
        sync_manager = SyncManager.objects.create(alliance=self.alliance_1)
        # when
        result = sync_manager.update_from_esi()
        # then
        self.assertFalse(result)
        sync_manager.refresh_from_db()
        self.assertEqual(sync_manager.last_error, SyncManager.Error.NO_CHARACTER)

    def test_should_abort_when_insufficient_permission(self):
        # given
        sync_manager = SyncManager.objects.create(
            alliance=self.alliance_1, character_ownership=self.main_ownership_2
        )
        # when
        result = sync_manager.update_from_esi()
        # then
        self.assertFalse(result)
        sync_manager.refresh_from_db()
        self.assertEqual(
            sync_manager.last_error, SyncManager.Error.INSUFFICIENT_PERMISSIONS
        )

    @patch(MODELS_PATH + ".Token")
    def test_should_report_error_when_character_has_no_token(self, mock_Token):
        # given
        mock_Token.objects.filter.return_value.require_scopes.return_value.require_valid.return_value.first.return_value = (
            None
        )
        sync_manager = SyncManager.objects.create(
            alliance=self.alliance_1, character_ownership=self.main_ownership_1
        )
        # when
        result = sync_manager.update_from_esi()
        # then
        sync_manager.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(sync_manager.last_error, SyncManager.Error.TOKEN_INVALID)

    @patch(MODELS_PATH + ".Token")
    def test_should_report_error_when_token_is_expired(self, mock_Token):
        # given
        mock_Token.objects.filter.side_effect = TokenExpiredError()
        sync_manager = SyncManager.objects.create(
            alliance=self.alliance_1, character_ownership=self.main_ownership_1
        )
        SyncedCharacter.objects.create(
            character_ownership=self.alt_ownership, manager=sync_manager
        )
        # when
        result = sync_manager.update_from_esi()
        # then
        sync_manager.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(sync_manager.last_error, SyncManager.Error.TOKEN_EXPIRED)

    @patch(MODELS_PATH + ".Token")
    def test_should_report_error_when_token_is_invalid(self, mock_Token):
        # given
        mock_Token.objects.filter.side_effect = TokenInvalidError()
        sync_manager = SyncManager.objects.create(
            alliance=self.alliance_1, character_ownership=self.main_ownership_1
        )
        SyncedCharacter.objects.create(
            character_ownership=self.alt_ownership, manager=sync_manager
        )
        # when
        result = sync_manager.update_from_esi()
        # then
        sync_manager.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(sync_manager.last_error, SyncManager.Error.TOKEN_INVALID)

    @patch(MODELS_PATH + ".Token")
    @patch(MODELS_PATH + ".esi")
    def test_should_sync_contacts(self, mock_esi, mock_Token):
        # given
        sync_manager = SyncManager.objects.create(
            alliance=self.alliance_1, character_ownership=self.main_ownership_1
        )
        SyncedCharacter.objects.create(
            character_ownership=self.alt_ownership, manager=sync_manager
        )
        with patch(MODELS_PATH + ".STANDINGSSYNC_ADD_WAR_TARGETS", False):
            # when
            self._run_sync(sync_manager, mock_esi, mock_Token)
        # then (continued)
        contact = sync_manager.contacts.get(eve_entity_id=3015)
        self.assertEqual(contact.standing, 10.0)
        self.assertFalse(contact.is_war_target)

    @patch(MODELS_PATH + ".Token")
    @patch(MODELS_PATH + ".esi")
    def test_should_sync_contacts_and_war_targets(self, mock_esi, mock_Token):
        # given
        sync_manager = SyncManager.objects.create(
            alliance=self.alliance_1, character_ownership=self.main_ownership_1
        )
        SyncedCharacter.objects.create(
            character_ownership=self.alt_ownership, manager=sync_manager
        )
        EveWar.objects.create(
            id=8,
            aggressor=EveEntity.objects.get(id=3015),
            defender=EveEntity.objects.get(id=3001),
            declared=now() - dt.timedelta(days=3),
            started=now() - dt.timedelta(days=2),
            is_mutual=False,
            is_open_for_allies=False,
        )

        with patch(MODELS_PATH + ".STANDINGSSYNC_ADD_WAR_TARGETS", True):
            # when
            self._run_sync(sync_manager, mock_esi, mock_Token)
        # then (continued)
        contact = sync_manager.contacts.get(eve_entity_id=3015)
        self.assertEqual(contact.standing, -10.0)
        self.assertTrue(contact.is_war_target)

    def _run_sync(self, sync_manager, mock_esi, mock_Token):
        def esi_get_alliances_alliance_id_contacts(*args, **kwargs):
            return BravadoOperationStub(ALLIANCE_CONTACTS)

        # given
        mock_esi.client.Contacts.get_alliances_alliance_id_contacts.side_effect = (
            esi_get_alliances_alliance_id_contacts
        )
        mock_Token.objects.filter.return_value.require_scopes.return_value.require_valid.return_value.first.return_value = Mock(
            spec=Token
        )
        # when
        result = sync_manager.update_from_esi()
        # then
        self.assertTrue(result)
        sync_manager.refresh_from_db()
        self.assertEqual(sync_manager.last_error, SyncManager.Error.NONE)
        expected_contact_ids = {x["contact_id"] for x in ALLIANCE_CONTACTS}
        expected_contact_ids.add(self.character_1.alliance_id)
        result_contact_ids = set(
            sync_manager.contacts.values_list("eve_entity_id", flat=True)
        )
        self.assertSetEqual(expected_contact_ids, result_contact_ids)
        return sync_manager


class EsiContact:
    class ContactType(Enum):
        CHARACTER = "character"
        CORPORATION = "corporation"
        ALLIANCE = "alliance"

    def __init__(
        self,
        contact_id: int,
        contact_type: str,
        standing: float,
        label_ids: list = None,
    ) -> None:
        if contact_type not in self.ContactType:
            raise ValueError(f"Invalid contact_type: {contact_type}")

        self._contact_id = int(contact_id)
        self._contact_type = contact_type
        self.standing = float(standing)
        self.label_ids = list(label_ids) if label_ids else []

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(contact_id={self.contact_id}, "
            f"contact_type={self.contact_type}, standing={self.standing}, "
            f"label_ids={self.label_ids}"
        )

    def __key(self):
        return (
            self.contact_id,
            self.contact_type,
            self.standing,
            tuple(self.label_ids),
        )

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        if isinstance(other, type(self)):
            return self.__key() == other.__key()
        return NotImplemented

    @property
    def contact_id(self):
        return self._contact_id

    @property
    def contact_type(self):
        return self._contact_type

    def to_esi_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "contact_type": self.ContactType(self.contact_type),
            "standing": self.standing,
            "label_ids": self.label_ids,
        }


class EsiCharacterContacts:
    """Simulates the contacts for a character on ESI"""

    def __init__(self) -> None:
        self._contacts = dict()
        self._labels = dict()

    def setup_contacts(self, character_id: int, contacts: list):
        self._contacts[character_id] = dict()
        for contact in contacts:
            self._contacts[character_id][contact.contact_id] = copy.deepcopy(contact)

    def setup_labels(self, character_id: int, labels: dict):
        self._contacts[character_id] = dict(labels)

    def contacts(self, character_id: int) -> dict:
        return self._contacts[character_id].values()

    def labels(self, character_id: int) -> dict:
        return self._labels[character_id]

    def esi_get_characters_character_id_contacts(self, character_id, token, page=None):
        contacts = [obj.to_esi_dict() for obj in self._contacts[character_id].values()]
        return BravadoOperationStub(contacts)

    def esi_get_characters_character_id_contacts_labels(
        self, character_id, token, page=None
    ):
        labels = [
            {"label_id": k, "label_name": v}
            for k, v in self._labels[character_id].items()
        ]
        return BravadoOperationStub(labels)

    def esi_post_characters_character_id_contacts(
        self, character_id, contact_ids, standing, token
    ):
        contact_type_map = {
            1: EsiContact.ContactType.CHARACTER,
            2: EsiContact.ContactType.CORPORATION,
            3: EsiContact.ContactType.ALLIANCE,
        }
        for contact_id in contact_ids:
            contact_type = contact_type_map[contact_id // 1000]
            self._contacts[character_id][contact_id] = EsiContact(
                contact_id=contact_id, contact_type=contact_type, standing=standing
            )
        return BravadoOperationStub([])

    def esi_put_characters_character_id_contacts(
        self, character_id, contact_ids, standing, token
    ):
        for contact_id in contact_ids:
            self._contacts[character_id][contact_id].standing = standing
        return BravadoOperationStub([])

    def esi_delete_characters_character_id_contacts(
        self, character_id, contact_ids, token
    ):
        for contact_id in contact_ids:
            del self._contacts[character_id][contact_id]
        return BravadoOperationStub([])


class TestSyncCharacter(LoadTestDataMixin, NoSocketsTestCase):
    CHARACTER_CONTACTS = [
        EsiContact(1014, EsiContact.ContactType.CHARACTER, standing=10.0),
        EsiContact(2011, EsiContact.ContactType.CORPORATION, standing=5.0),
        EsiContact(3011, EsiContact.ContactType.ALLIANCE, standing=-10.0),
    ]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # 1 user with 1 alt character
        cls.user_1 = create_test_user(cls.character_1)
        cls.main_ownership_1 = CharacterOwnership.objects.get(
            character=cls.character_1, user=cls.user_1
        )
        cls.alt_ownership_2 = CharacterOwnership.objects.create(
            character=cls.character_2, owner_hash="x2", user=cls.user_1
        )
        cls.alt_ownership_3 = CharacterOwnership.objects.create(
            character=cls.character_3, owner_hash="x3", user=cls.user_1
        )
        # sync manager with contacts
        cls.sync_manager = SyncManager.objects.create(
            alliance=cls.alliance_1,
            character_ownership=cls.main_ownership_1,
            version_hash="new",
        )

    @staticmethod
    def eve_contact_2_esi_contact(eve_contact):
        map_category_2_type = {
            EveEntity.Category.CHARACTER: EsiContact.ContactType.CHARACTER,
            EveEntity.Category.CORPORATION: EsiContact.ContactType.CORPORATION,
            EveEntity.Category.ALLIANCE: EsiContact.ContactType.ALLIANCE,
        }
        return EsiContact(
            contact_id=eve_contact.eve_entity_id,
            contact_type=map_category_2_type[eve_contact.eve_entity.category],
            standing=eve_contact.standing,
        )

    def setUp(self) -> None:
        self.maxDiff = None
        self.sync_manager.contacts.all().delete()
        for contact in ALLIANCE_CONTACTS:
            EveContact.objects.create(
                manager=self.sync_manager,
                eve_entity=EveEntity.objects.get(id=contact["contact_id"]),
                standing=contact["standing"],
                is_war_target=False,
            )
        self.sync_manager.contacts.filter(eve_entity_id__in=[1014, 3013]).update(
            is_war_target=True, standing=-10.0
        )
        self.alliance_contacts = [
            self.eve_contact_2_esi_contact(obj)
            for obj in self.sync_manager.contacts.all()
        ]

        self.synced_character_2 = SyncedCharacter.objects.create(
            character_ownership=self.alt_ownership_2, manager=self.sync_manager
        )
        self.synced_character_3 = SyncedCharacter.objects.create(
            character_ownership=self.alt_ownership_3, manager=self.sync_manager
        )

    def test_get_last_error_message_after_sync(self):
        self.synced_character_2.last_sync = now()
        self.synced_character_2.last_error = SyncedCharacter.Error.NONE
        expected = "OK"
        self.assertEqual(self.synced_character_2.get_status_message(), expected)

        self.synced_character_2.last_error = SyncedCharacter.Error.TOKEN_EXPIRED
        expected = "Expired token"
        self.assertEqual(self.synced_character_2.get_status_message(), expected)

    def test_get_last_error_message_no_sync(self):
        self.synced_character_2.last_sync = None
        self.synced_character_2.last_error = SyncedCharacter.Error.NONE
        expected = "Not synced yet"
        self.assertEqual(self.synced_character_2.get_status_message(), expected)

        self.synced_character_2.last_error = SyncedCharacter.Error.TOKEN_EXPIRED
        expected = "Expired token"
        self.assertEqual(self.synced_character_2.get_status_message(), expected)

    def test_set_sync_status(self):
        self.synced_character_2.last_error = SyncManager.Error.NONE
        self.synced_character_2.last_sync = None

        self.synced_character_2.set_sync_status(SyncManager.Error.TOKEN_INVALID)
        self.synced_character_2.refresh_from_db()

        self.assertEqual(
            self.synced_character_2.last_error, SyncManager.Error.TOKEN_INVALID
        )
        self.assertIsNotNone(self.synced_character_2.last_sync)

    @patch(MODELS_PATH + ".STANDINGSSYNC_REPLACE_CONTACTS", True)
    @patch(MODELS_PATH + ".STANDINGSSYNC_CHAR_MIN_STANDING", 0.01)
    @patch(MODELS_PATH + ".Token")
    @patch(MODELS_PATH + ".esi")
    def test_should_replace_all_contacts_1(self, mock_esi, mock_Token):
        """run normal sync for a character which has blue standing"""
        # given
        character_id = (
            self.synced_character_2.character_ownership.character.character_id
        )
        esi_character_contacts = EsiCharacterContacts()
        esi_character_contacts.setup_contacts(character_id, self.CHARACTER_CONTACTS)
        # when
        result = self._run_sync(
            mock_esi, mock_Token, self.synced_character_2, esi_character_contacts
        )
        # then
        self.assertTrue(result)
        self.assertEqual(self.synced_character_2.last_error, SyncedCharacter.Error.NONE)
        self.assertSetEqual(
            set(esi_character_contacts.contacts(character_id)),
            set(self.alliance_contacts),
        )

    @patch(MODELS_PATH + ".STANDINGSSYNC_REPLACE_CONTACTS", True)
    @patch(MODELS_PATH + ".STANDINGSSYNC_CHAR_MIN_STANDING", 0.0)
    @patch(MODELS_PATH + ".Token")
    @patch(MODELS_PATH + ".esi")
    def test_should_replace_all_contacts_2(self, mock_esi, mock_Token):
        """run normal sync for a character which has no standing and allow neutrals"""
        # given
        character_id = (
            self.synced_character_3.character_ownership.character.character_id
        )
        esi_character_contacts = EsiCharacterContacts()
        esi_character_contacts.setup_contacts(character_id, self.CHARACTER_CONTACTS)
        # when
        result = self._run_sync(
            mock_esi, mock_Token, self.synced_character_3, esi_character_contacts
        )
        # then
        self.assertTrue(result)
        self.assertEqual(self.synced_character_3.last_error, SyncedCharacter.Error.NONE)
        self.assertSetEqual(
            set(esi_character_contacts.contacts(character_id)),
            set(self.alliance_contacts),
        )

    @patch(MODELS_PATH + ".STANDINGSSYNC_ADD_WAR_TARGETS", True)
    @patch(MODELS_PATH + ".STANDINGSSYNC_REPLACE_CONTACTS", False)
    @patch(MODELS_PATH + ".STANDINGSSYNC_CHAR_MIN_STANDING", 0.01)
    @patch(MODELS_PATH + ".Token")
    @patch(MODELS_PATH + ".esi")
    def test_should_update_war_targets_only(self, mock_esi, mock_Token):
        # given
        character_id = (
            self.synced_character_2.character_ownership.character.character_id
        )
        esi_character_contacts = EsiCharacterContacts()
        esi_character_contacts.setup_contacts(character_id, self.CHARACTER_CONTACTS)
        # when
        result = self._run_sync(
            mock_esi, mock_Token, self.synced_character_2, esi_character_contacts
        )
        # then
        self.assertTrue(result)
        self.assertEqual(self.synced_character_2.last_error, SyncedCharacter.Error.NONE)
        expected = {
            EsiContact(1014, EsiContact.ContactType.CHARACTER, standing=-10.0),
            EsiContact(2011, EsiContact.ContactType.CORPORATION, standing=5.0),
            EsiContact(3011, EsiContact.ContactType.ALLIANCE, standing=-10.0),
            EsiContact(3013, EsiContact.ContactType.ALLIANCE, standing=-10.0),
        }
        self.assertSetEqual(
            set(esi_character_contacts.contacts(character_id)),
            expected,
        )

    @staticmethod
    def _run_sync(mock_esi, mock_Token, synced_character, esi_character_contacts):
        # given
        mock_esi.client.Contacts.get_characters_character_id_contacts.side_effect = (
            esi_character_contacts.esi_get_characters_character_id_contacts
        )
        mock_esi.client.Contacts.delete_characters_character_id_contacts.side_effect = (
            esi_character_contacts.esi_delete_characters_character_id_contacts
        )
        mock_esi.client.Contacts.post_characters_character_id_contacts = (
            esi_character_contacts.esi_post_characters_character_id_contacts
        )
        mock_esi.client.Contacts.put_characters_character_id_contacts = (
            esi_character_contacts.esi_put_characters_character_id_contacts
        )
        mock_Token.objects.filter = Mock()
        synced_character.character_ownership.user = (
            AuthUtils.add_permission_to_user_by_name(
                "standingssync.add_syncedcharacter",
                synced_character.character_ownership.user,
            )
        )
        # when
        result = synced_character.update()
        if result:
            synced_character.refresh_from_db()
        return result

    def test_should_deactivate_when_insufficient_permission(self):
        # when
        result = self.synced_character_2.update()
        # then
        self.assertFalse(result)
        self.assertFalse(
            SyncedCharacter.objects.filter(pk=self.synced_character_2.pk).exists()
        )

    @patch(MODELS_PATH + ".Token")
    def test_should_deactivate_when_token_is_invalid(self, mock_Token):
        # given
        mock_Token.objects.filter.side_effect = TokenInvalidError()
        AuthUtils.add_permission_to_user_by_name(
            "standingssync.add_syncedcharacter", self.user_1
        )
        # when
        result = self.synced_character_2.update()
        # then
        self.assertFalse(result)
        self.assertFalse(
            SyncedCharacter.objects.filter(pk=self.synced_character_2.pk).exists()
        )

    @patch(MODELS_PATH + ".Token")
    def test_should_deactivate_when_token_is_expired(self, mock_Token):
        # given
        mock_Token.objects.filter.side_effect = TokenExpiredError()
        AuthUtils.add_permission_to_user_by_name(
            "standingssync.add_syncedcharacter", self.user_1
        )
        # when
        result = self.synced_character_2.update()
        # then
        self.assertFalse(result)
        self.assertFalse(
            SyncedCharacter.objects.filter(pk=self.synced_character_2.pk).exists()
        )

    @patch(MODELS_PATH + ".STANDINGSSYNC_CHAR_MIN_STANDING", 0.1)
    @patch(MODELS_PATH + ".Token")
    def test_should_deactivate_when_character_has_no_standing(self, mock_Token):
        # given
        mock_Token.objects.filter.return_value = Mock()
        AuthUtils.add_permission_to_user_by_name(
            "standingssync.add_syncedcharacter", self.user_1
        )
        contact = self.sync_manager.contacts.get(
            eve_entity_id=self.character_2.character_id
        )
        contact.standing = -10
        contact.save()
        # when
        result = self.synced_character_2.update()
        # then
        self.assertFalse(result)
        self.assertFalse(
            SyncedCharacter.objects.filter(pk=self.synced_character_2.pk).exists()
        )


class TestEveContactManager(LoadTestDataMixin, NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # 1 user with 1 alt character
        cls.user_1 = create_test_user(cls.character_1)
        cls.main_ownership_1 = CharacterOwnership.objects.get(
            character=cls.character_1, user=cls.user_1
        )
        cls.alt_ownership = CharacterOwnership.objects.create(
            character=cls.character_2, owner_hash="x2", user=cls.user_1
        )

        # sync manager with contacts
        cls.sync_manager = SyncManager.objects.create(
            alliance=cls.alliance_1,
            character_ownership=cls.main_ownership_1,
            version_hash="new",
        )
        for contact in ALLIANCE_CONTACTS:
            EveContact.objects.create(
                manager=cls.sync_manager,
                eve_entity=EveEntity.objects.get(id=contact["contact_id"]),
                standing=contact["standing"],
                is_war_target=False,
            )

        # sync char
        cls.synced_character = SyncedCharacter.objects.create(
            character_ownership=cls.alt_ownership, manager=cls.sync_manager
        )

    def test_grouped_by_standing(self):
        c = {
            int(x.eve_entity_id): x
            for x in self.sync_manager.contacts.order_by("eve_entity_id")
        }
        expected = {
            -10.0: {c[1005], c[1012], c[3011], c[2011]},
            -5.0: {c[1013], c[3012], c[2012]},
            0.0: {c[1014], c[3013], c[2014]},
            5.0: {c[1015], c[3014], c[2013]},
            10.0: {c[1002], c[1004], c[1016], c[3015], c[2015]},
        }
        result = self.sync_manager.contacts.all().grouped_by_standing()
        self.maxDiff = None
        self.assertDictEqual(result, expected)


class TestEveEntityManagerGetOrCreateFromEsiInfo(NoSocketsTestCase):
    def test_should_return_corporation(self):
        # given
        info = {"corporation_id": 2001}
        # when
        obj, created = EveEntity.objects.get_or_create_from_esi_info(info)
        # then
        self.assertEqual(obj.id, 2001)
        self.assertEqual(obj.category, EveEntity.Category.CORPORATION)

    def test_should_return_alliance(self):
        # given
        info = {"alliance_id": 3001}
        # when
        obj, created = EveEntity.objects.get_or_create_from_esi_info(info)
        # then
        self.assertEqual(obj.id, 3001)
        self.assertEqual(obj.category, EveEntity.Category.ALLIANCE)


class TestEveWarManagerActiveWars(LoadTestDataMixin, NoSocketsTestCase):
    def test_should_return_started_war(self):
        # given
        EveWar.objects.create(
            id=8,
            aggressor=EveEntity.objects.get(id=3011),
            defender=EveEntity.objects.get(id=3001),
            declared=now() - dt.timedelta(days=3),
            started=now() - dt.timedelta(days=2),
            is_mutual=False,
            is_open_for_allies=False,
        )
        # when
        result = EveWar.objects.active_wars()
        # then
        self.assertEqual(result.count(), 1)
        self.assertEqual(result.first().id, 8)

    def test_should_return_war_about_to_finish(self):
        # given
        EveWar.objects.create(
            id=8,
            aggressor=EveEntity.objects.get(id=3011),
            defender=EveEntity.objects.get(id=3001),
            declared=now() - dt.timedelta(days=3),
            started=now() - dt.timedelta(days=2),
            finished=now() + dt.timedelta(days=1),
            is_mutual=False,
            is_open_for_allies=False,
        )
        # when
        result = EveWar.objects.active_wars()
        # then
        self.assertEqual(result.count(), 1)
        self.assertEqual(result.first().id, 8)

    def test_should_not_return_finished_war(self):
        # given
        EveWar.objects.create(
            id=8,
            aggressor=EveEntity.objects.get(id=3011),
            defender=EveEntity.objects.get(id=3001),
            declared=now() - dt.timedelta(days=3),
            started=now() - dt.timedelta(days=2),
            finished=now() - dt.timedelta(days=1),
            is_mutual=False,
            is_open_for_allies=False,
        )
        # when
        result = EveWar.objects.active_wars()
        # then
        self.assertEqual(result.count(), 0)

    def test_should_not_return_war_not_yet_started(self):
        # given
        EveWar.objects.create(
            id=8,
            aggressor=EveEntity.objects.get(id=3011),
            defender=EveEntity.objects.get(id=3001),
            declared=now() - dt.timedelta(days=1),
            started=now() + dt.timedelta(hours=4),
            is_mutual=False,
            is_open_for_allies=False,
        )
        # when
        result = EveWar.objects.active_wars()
        # then
        self.assertEqual(result.count(), 0)


class TestEveWarManager(LoadTestDataMixin, NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # given
        cls.war_declared = now() - dt.timedelta(days=3)
        cls.war_started = now() - dt.timedelta(days=2)
        war = EveWar.objects.create(
            id=8,
            aggressor=EveEntity.objects.get(id=3011),
            defender=EveEntity.objects.get(id=3001),
            declared=cls.war_declared,
            started=cls.war_started,
            is_mutual=False,
            is_open_for_allies=False,
        )
        war.allies.add(EveEntity.objects.get(id=3012))

    def test_should_return_defender_and_allies_for_aggressor(self):
        # when
        result = EveWar.objects.war_targets(3011)
        # then
        self.assertSetEqual({obj.id for obj in result}, {3001, 3012})

    def test_should_return_aggressor_for_defender(self):
        # when
        result = EveWar.objects.war_targets(3001)
        # then
        self.assertSetEqual({obj.id for obj in result}, {3011})

    def test_should_return_aggressor_for_ally(self):
        # when
        result = EveWar.objects.war_targets(3012)
        # then
        self.assertSetEqual({obj.id for obj in result}, {3011})

    def test_should_return_finished_wars(self):
        # given
        EveWar.objects.create(
            id=2,  # finished in the past
            aggressor=EveEntity.objects.get(id=3011),
            defender=EveEntity.objects.get(id=3001),
            declared=now() - dt.timedelta(days=5),
            started=now() - dt.timedelta(days=4),
            finished=now() - dt.timedelta(days=2),
            is_mutual=False,
            is_open_for_allies=False,
        )
        EveWar.objects.create(
            id=3,  # about to finish
            aggressor=EveEntity.objects.get(id=3011),
            defender=EveEntity.objects.get(id=3001),
            declared=now() - dt.timedelta(days=5),
            started=now() - dt.timedelta(days=4),
            finished=now() + dt.timedelta(days=1),
            is_mutual=False,
            is_open_for_allies=False,
        )
        EveWar.objects.create(
            id=4,  # not yet started
            aggressor=EveEntity.objects.get(id=3011),
            defender=EveEntity.objects.get(id=3001),
            declared=now() - dt.timedelta(days=1),
            started=now() + dt.timedelta(days=1),
            is_mutual=False,
            is_open_for_allies=False,
        )
        # when
        result = EveWar.objects.finished_wars()
        # then
        self.assertSetEqual({obj.id for obj in result}, {2})

    @patch(MANAGERS_PATH + ".esi")
    def test_should_create_full_war_object_from_esi_1(self, mock_esi):
        # given
        declared = now() - dt.timedelta(days=5)
        started = now() - dt.timedelta(days=4)
        finished = now() + dt.timedelta(days=1)
        retracted = now()
        esi_data = {
            "aggressor": {
                "alliance_id": 3001,
                "isk_destroyed": 0,
                "ships_killed": 0,
            },
            "allies": [{"alliance_id": 3003}, {"corporation_id": 2003}],
            "declared": declared,
            "defender": {
                "alliance_id": 3002,
                "isk_destroyed": 0,
                "ships_killed": 0,
            },
            "finished": finished,
            "id": 1,
            "mutual": False,
            "open_for_allies": True,
            "retracted": retracted,
            "started": started,
        }
        mock_esi.client.Wars.get_wars_war_id.return_value = BravadoOperationStub(
            esi_data
        )
        # when
        EveWar.objects.update_from_esi(id=1)
        # then
        self.assertTrue(EveWar.objects.filter(id=1).exists())
        war = EveWar.objects.get(id=1)
        self.assertEqual(war.aggressor.id, 3001)
        self.assertEqual(set(war.allies.values_list("id", flat=True)), {2003, 3003})
        self.assertEqual(war.declared, declared)
        self.assertEqual(war.defender.id, 3002)
        self.assertEqual(war.finished, finished)
        self.assertFalse(war.is_mutual)
        self.assertTrue(war.is_open_for_allies)
        self.assertEqual(war.retracted, retracted)
        self.assertEqual(war.started, started)

    @patch(MANAGERS_PATH + ".esi")
    def test_should_create_full_war_object_from_esi_2(self, mock_esi):
        # given
        declared = now() - dt.timedelta(days=5)
        started = now() - dt.timedelta(days=4)
        esi_data = {
            "aggressor": {
                "alliance_id": 3001,
                "isk_destroyed": 0,
                "ships_killed": 0,
            },
            "allies": None,
            "declared": declared,
            "defender": {
                "alliance_id": 3002,
                "isk_destroyed": 0,
                "ships_killed": 0,
            },
            "finished": None,
            "id": 1,
            "mutual": False,
            "open_for_allies": True,
            "retracted": None,
            "started": started,
        }
        mock_esi.client.Wars.get_wars_war_id.return_value = BravadoOperationStub(
            esi_data
        )
        # when
        EveWar.objects.update_from_esi(id=1)
        # then
        self.assertTrue(EveWar.objects.filter(id=1).exists())
        war = EveWar.objects.get(id=1)
        self.assertEqual(war.aggressor.id, 3001)
        self.assertEqual(war.allies.count(), 0)
        self.assertEqual(war.declared, declared)
        self.assertEqual(war.defender.id, 3002)
        self.assertIsNone(war.finished)
        self.assertFalse(war.is_mutual)
        self.assertTrue(war.is_open_for_allies)
        self.assertIsNone(war.retracted)
        self.assertEqual(war.started, started)

    @patch(MANAGERS_PATH + ".esi")
    def test_should_not_create_object_from_esi_for_finished_war(self, mock_esi):
        # given
        declared = now() - dt.timedelta(days=5)
        started = now() - dt.timedelta(days=4)
        finished = now() - dt.timedelta(days=1)
        esi_data = {
            "aggressor": {
                "alliance_id": 3001,
                "isk_destroyed": 0,
                "ships_killed": 0,
            },
            "allies": [{"alliance_id": 3003}, {"corporation_id": 2003}],
            "declared": declared,
            "defender": {
                "alliance_id": 3002,
                "isk_destroyed": 0,
                "ships_killed": 0,
            },
            "finished": finished,
            "id": 1,
            "mutual": False,
            "open_for_allies": True,
            "retracted": None,
            "started": started,
        }
        mock_esi.client.Wars.get_wars_war_id.return_value = BravadoOperationStub(
            esi_data
        )
        # when
        EveWar.objects.update_from_esi(id=1)
        # then
        self.assertFalse(EveWar.objects.filter(id=1).exists())

    @patch(MANAGERS_PATH + ".esi")
    def test_should_update_existing_war_from_esi(self, mock_esi):
        # given
        finished = now() + dt.timedelta(days=1)
        retracted = now()
        esi_data = {
            "aggressor": {
                "alliance_id": 3011,
                "isk_destroyed": 0,
                "ships_killed": 0,
            },
            "allies": [{"alliance_id": 3003}, {"corporation_id": 2003}],
            "declared": self.war_declared,
            "defender": {
                "alliance_id": 3001,
                "isk_destroyed": 0,
                "ships_killed": 0,
            },
            "finished": finished,
            "id": 8,
            "mutual": True,
            "open_for_allies": True,
            "retracted": retracted,
            "started": self.war_started,
        }
        mock_esi.client.Wars.get_wars_war_id.return_value = BravadoOperationStub(
            esi_data
        )
        # when
        EveWar.objects.update_from_esi(id=8)
        # then
        self.assertTrue(EveWar.objects.filter(id=8).exists())
        war = EveWar.objects.get(id=8)
        self.assertEqual(war.aggressor.id, 3011)
        self.assertEqual(set(war.allies.values_list("id", flat=True)), {2003, 3003})
        self.assertEqual(war.declared, self.war_declared)
        self.assertEqual(war.defender.id, 3001)
        self.assertEqual(war.finished, finished)
        self.assertTrue(war.is_mutual)
        self.assertTrue(war.is_open_for_allies)
        self.assertEqual(war.retracted, retracted)
        self.assertEqual(war.started, self.war_started)


class TestEveEntity(LoadTestDataMixin, NoSocketsTestCase):
    def test_should_return_esi_dict_for_character(self):
        # given
        obj = EveEntity.objects.get(id=1001)
        # when
        result = obj.to_esi_dict(5.0)
        # then
        self.assertDictEqual(
            result, {"contact_id": 1001, "contact_type": "character", "standing": 5.0}
        )

    def test_should_return_esi_dict_for_corporation(self):
        # given
        obj = EveEntity.objects.get(id=2001)
        # when
        result = obj.to_esi_dict(2.0)
        # then
        self.assertDictEqual(
            result, {"contact_id": 2001, "contact_type": "corporation", "standing": 2.0}
        )

    def test_should_return_esi_dict_for_alliance(self):
        # given
        obj = EveEntity.objects.get(id=3001)
        # when
        result = obj.to_esi_dict(-2.0)
        # then
        self.assertDictEqual(
            result, {"contact_id": 3001, "contact_type": "alliance", "standing": -2.0}
        )
