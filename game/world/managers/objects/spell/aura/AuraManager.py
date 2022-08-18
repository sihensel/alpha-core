from struct import pack
from typing import Optional

from database.dbc.DbcDatabaseManager import DbcDatabaseManager
from game.world.managers.objects.spell import ExtendedSpellData
from game.world.managers.objects.spell.aura.AppliedAura import AppliedAura
from game.world.managers.objects.spell.aura.AuraEffectHandler import AuraEffectHandler
from game.world.managers.objects.spell.CastingSpell import CastingSpell
from network.packet.PacketWriter import PacketWriter, OpCode
from utils.constants.MiscCodes import ObjectTypeFlags, ProcFlags, ObjectTypeIds
from utils.constants.SpellCodes import AuraTypes, AuraSlots, SpellAuraInterruptFlags, SpellAttributes, \
    SpellAttributesEx, SpellEffects
from utils.constants.UnitCodes import UnitFlags, StandState
from utils.constants.UpdateFields import UnitFields


class AuraManager:
    def __init__(self, unit_mgr):
        self.unit_mgr = unit_mgr
        self.active_auras = {}  # (int: Aura) to have persistent indices.
        self.current_flags = 0x0

    def apply_spell_effect_aura(self, caster, casting_spell, spell_effect):
        aura = AppliedAura(caster, casting_spell, spell_effect, self.unit_mgr)
        self.add_aura(aura)

    def add_aura(self, aura):
        can_apply = self.can_apply_aura(aura) and self.remove_colliding_effects(aura)
        if not can_apply:
            return

        # Application threat and negative aura application interrupts.
        if aura.harmful:
            # Add threat for non-player targets against unit casters.
            if aura.caster.object_type_mask & ObjectTypeFlags.TYPE_UNIT and \
                    self.unit_mgr.get_type_id() == ObjectTypeIds.ID_UNIT and aura.source_spell.generates_threat():
                # TODO: Threat calculation.
                self.unit_mgr.threat_manager.add_threat(aura.caster, 10)

            self.check_aura_interrupts(negative_aura_applied=True)

        applied_similar_auras = self.get_similar_applied_auras(aura, accept_all_ranks=False, accept_all_sources=False)
        is_refresh = len(applied_similar_auras) > 0
        if is_refresh > 0:
            # Only one similar aura from the same source can be applied.
            # Lower ranks are removed by remove_colliding_effects.
            similar_aura = applied_similar_auras[0]

            if aura.can_stack and similar_aura.applied_stacks < similar_aura.max_stacks:
                similar_aura.applied_stacks += 1  # Add a stack if the aura isn't at max already

            similar_aura.spell_effect.start_aura_duration(overwrite=True)  # Refresh duration

            # Note that this aura will not be actually applied.
            # Index and stacks are copied for sending information and updating effect points.
            aura.applied_stacks = similar_aura.applied_stacks
            aura.index = similar_aura.index
        else:
            aura.index = self.get_next_aura_index(aura)
            self.active_auras[aura.index] = aura

        # Handle effects after possible stack increase/refresh to update stats properly.
        AuraEffectHandler.handle_aura_effect_change(aura, aura.target)

        self.write_aura_to_unit(aura, is_refresh=is_refresh)

    def update(self, timestamp):
        for aura in list(self.active_auras.values()):
            aura.update(timestamp)  # Update duration and handle periodic effects.
            if aura.has_duration() and aura.get_duration() <= 0:
                self.remove_aura(aura)

    def can_apply_aura(self, aura) -> bool:
        if aura.spell_effect.aura_type == AuraTypes.SPELL_AURA_MOD_SHAPESHIFT and \
                len(self.get_auras_by_spell_id(aura.spell_id)) > 0:
            return False  # Don't apply same shapeshift effect if it already exists.

        # Stronger effect applied.
        similar_applied_auras = self.get_similar_applied_auras(aura, accept_all_ranks=True, accept_all_sources=False)
        if len(similar_applied_auras) > 0:
            similar_applied = similar_applied_auras[0]  # Only one similar aura from one source can be applied.
            applied_rank = DbcDatabaseManager.SpellHolder.spell_get_rank_by_spell(similar_applied.source_spell.spell_entry)
            new_rank = DbcDatabaseManager.SpellHolder.spell_get_rank_by_spell(aura.source_spell.spell_entry)
            if applied_rank > new_rank:
                return False

        return True

    def are_spell_effects_applicable(self, casting_spell):
        for spell_effect in casting_spell.get_effects():
            if spell_effect.effect_type == SpellEffects.SPELL_EFFECT_SUMMON_MOUNT and \
                    len(self.get_auras_by_type(AuraTypes.SPELL_AURA_MOUNTED)):
                # Special case of mounting via spell effect when the player already has a mount aura applied.
                # This interaction does not currently work,
                # as the mount aura is removed via aura interrupts on cast (fixable?).
                # This results in the spell effect's mount applying instead of dismounting the player,
                # visually leaving the player on the aura-applied mount.

                return False

            if spell_effect.effect_type != SpellEffects.SPELL_EFFECT_APPLY_AURA:
                continue

            aura = AppliedAura(casting_spell.spell_caster, casting_spell, spell_effect, self.unit_mgr)
            if not self.can_apply_aura(aura):
                return False
        return True

    def check_aura_interrupts(self, moved=False, turned=False, changed_stand_state=False, negative_aura_applied=False,
                              received_damage=False, cast_spell: Optional[CastingSpell] = None):
        # Add once movement information is passed to update.
        flag_cases = {
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_ENTER_COMBAT: self.unit_mgr.in_combat,
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_NOT_MOUNTED: self.unit_mgr.unit_flags & UnitFlags.UNIT_MASK_MOUNTED,
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_MOVE: moved,
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_TURNING: turned,
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_CAST: cast_spell is not None,
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_NEGATIVE_SPELL: negative_aura_applied,
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_DAMAGE: received_damage,
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_NOT_ABOVEWATER: self.unit_mgr.is_on_water(),
            SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_NOT_UNDERWATER: not self.unit_mgr.is_on_water(),
        }

        for aura in list(self.active_auras.values()):
            # Food buffs are not labeled and an interrupt for sitting does not exist.
            # Food/drink spells do claim that the player must remain seated.
            # In later versions an aurainterrupt exists for this purpose.
            if aura.source_spell.is_refreshment_spell() and changed_stand_state and \
                    self.unit_mgr.stand_state != StandState.UNIT_SITTING:
                self.remove_aura(aura)
                continue

            # Special case for stealth breaking.
            if aura.spell_effect.aura_type == AuraTypes.SPELL_AURA_MOD_STEALTH and \
                    cast_spell and not cast_spell.cast_breaks_stealth():
                flag_cases[SpellAuraInterruptFlags.AURA_INTERRUPT_FLAG_CAST] = None

            for flag, condition in flag_cases.items():
                if aura.interrupt_flags & flag and condition:
                    self.remove_aura(aura)
                    continue

    # Involved unit is the secondary unit in the proc event.
    # is_receiver is set to false if the player is causing damage and set to true if the player is taking damage.
    def check_aura_procs(self, involved_cast=None, killed_unit=False, damage_info=None, is_melee_swing=False):
        is_receiver = (damage_info and damage_info.target is self.unit_mgr) or \
                      (involved_cast and involved_cast.spell_caster is not self.unit_mgr)

        # Always pass the second unit as the effect target. The handler will choose the target based on the spell.
        if damage_info:
            effect_target = damage_info.attacker if is_receiver else damage_info.target
        elif involved_cast:
            # All targets for the spell could be passed, but this would only matter for ProcFlags.SPELL_CAST
            # SPELL_CAST is only used by one deprecated spell which will have the correct target in initial_target.
            effect_target = involved_cast.spell_caster if is_receiver else involved_cast.initial_target
        else:
            effect_target = self.unit_mgr

        flag_cases = {
            ProcFlags.DEAL_COMBAT_DMG: not is_receiver and damage_info and damage_info.total_damage > 0,  # -> cast on target.
            ProcFlags.TAKE_COMBAT_DMG: is_receiver and damage_info and damage_info.total_damage > 0,
            ProcFlags.KILL: killed_unit,
            ProcFlags.HEARTBEAT: False,  # Heartbeat effects are handled in their respective places on update - ignore the flag here.
            ProcFlags.DODGE: is_receiver and damage_info and damage_info.proc_victim & ProcFlags.DODGE,
            ProcFlags.PARRY: is_receiver and damage_info and damage_info.proc_victim & ProcFlags.PARRY,
            ProcFlags.BLOCK: is_receiver and damage_info and damage_info.proc_victim & ProcFlags.BLOCK,
            ProcFlags.SWING: not is_receiver and is_melee_swing,
            ProcFlags.SPELL_CAST: not is_receiver and involved_cast,  # Only used by zzOLDMind Bomb.
            ProcFlags.SPELL_HIT: is_receiver and involved_cast,
        }
        for aura in list(self.active_auras.values()):
            flags = aura.proc_flags
            if not flags:
                continue

            for proc_flag, condition in flag_cases.items():
                if proc_flag & flags and condition and aura.proc_charges != 0:  # Proc charges are set to -1 for auras with no charges so check for 0.
                    # Remove charge before trigger to avoid infinite loops with procs.
                    aura.proc_charges -= 1
                    AuraEffectHandler.handle_aura_effect_change(aura, effect_target, is_proc=True)

                    if aura.proc_charges == 0:
                        self.remove_aura(aura)

    def remove_colliding_effects(self, aura):
        # Special case with SpellEffect mounting and mounting by aura
        if aura.spell_effect.aura_type == AuraTypes.SPELL_AURA_MOUNTED and \
                aura.target.unit_flags & UnitFlags.UNIT_MASK_MOUNTED and not \
                self.get_auras_by_type(AuraTypes.SPELL_AURA_MOUNTED):
            AuraEffectHandler.handle_mounted(aura, aura.target, remove=True)  # Remove mount effect
            # If a mount aura would be applied but we dismount the unit, don't apply the new mount aura.
            return False

        aura_spell_template = aura.source_spell.spell_entry

        new_aura_name = aura_spell_template.Name_enUS
        new_aura_rank = DbcDatabaseManager.SpellHolder.spell_get_rank_by_spell(aura_spell_template)

        aura_effect_index = aura.spell_effect.effect_index
        caster_guid = aura.caster.guid

        for applied_aura in list(self.active_auras.values()):
            applied_spell_entry = applied_aura.source_spell.spell_entry
            applied_aura_name = applied_spell_entry.Name_enUS
            applied_aura_rank = DbcDatabaseManager.SpellHolder.spell_get_rank_by_spell(applied_spell_entry)

            # TODO Same effects but different spells (exclusivity groups)?

            # Note: This method ignores the case of a weaker spell being applied, as that is handled in can_apply_aura.
            is_similar_and_weaker = applied_aura.spell_effect.effect_index == aura_effect_index and \
                applied_aura_name == new_aura_name and applied_aura_rank < new_aura_rank

            are_exclusive_by_source = ExtendedSpellData.AuraSourceRestrictions.are_colliding_auras(aura.spell_id, applied_aura.spell_id)  # Paladin seals, warlock curses

            # Source doesn't matter for unique auras.
            is_unique = applied_aura.source_spell.spell_entry.AttributesEx & SpellAttributesEx.SPELL_ATTR_EX_AURA_UNIQUE or not aura.harmful  # Buffs are unique.
            is_stacking = applied_aura.can_stack
            is_same_but_different_aura_index = aura.spell_id == applied_aura.spell_id and aura.spell_effect.effect_index != applied_aura.spell_effect.effect_index

            casters_are_same = applied_aura.caster.guid == caster_guid
            if is_similar_and_weaker and (is_unique or casters_are_same and not is_stacking) or \
                    are_exclusive_by_source and casters_are_same and not is_same_but_different_aura_index:
                self.remove_aura(applied_aura)
                continue

            if applied_aura.spell_effect.aura_type == AuraTypes.SPELL_AURA_MOD_SHAPESHIFT and \
                    aura.spell_effect.aura_type == AuraTypes.SPELL_AURA_MOD_SHAPESHIFT:
                self.remove_aura(applied_aura)  # Player can only be in one shapeshift form.
                continue

        return True

    def has_aura_by_spell_id(self, spell_id):
        for aura in self.active_auras.values():
            if aura.spell_id == spell_id:
                return True
        return False

    def get_auras_by_spell_id(self, spell_id) -> list[AppliedAura]:
        auras = []
        for aura in self.active_auras.values():
            if aura.spell_id != spell_id:
                continue
            auras.append(aura)
        return auras

    def get_auras_by_type(self, aura_type) -> list[AppliedAura]:
        auras = []
        for aura in list(self.active_auras.values()):
            if aura.spell_effect.aura_type != aura_type:
                continue
            auras.append(aura)
        return auras

    def get_similar_applied_auras(self, aura, accept_all_ranks=True, accept_all_sources=True) -> list[AppliedAura]:
        aura_spell_template = aura.source_spell.spell_entry

        new_aura_name = aura_spell_template.Name_enUS
        new_aura_rank = DbcDatabaseManager.SpellHolder.spell_get_rank_by_spell(aura_spell_template)

        similar_auras = []

        for applied_aura in list(self.active_auras.values()):
            if applied_aura.spell_effect.effect_index != aura.spell_effect.effect_index:
                continue

            if not accept_all_sources and aura.caster != applied_aura.caster:
                continue

            applied_spell_entry = applied_aura.source_spell.spell_entry

            if applied_spell_entry.ID != aura_spell_template.ID:
                continue

            applied_aura_name = applied_spell_entry.Name_enUS
            applied_aura_rank = DbcDatabaseManager.SpellHolder.spell_get_rank_by_spell(applied_spell_entry)

            if applied_aura_name != new_aura_name or \
                    (applied_aura_rank != new_aura_rank and not accept_all_ranks):
                continue

            similar_auras.append(applied_aura)

        return similar_auras

    def remove_auras_by_type(self, aura_type):
        for aura in list(self.active_auras.values()):
            if aura.spell_effect.aura_type != aura_type:
                continue
            self.remove_aura(aura)

    def handle_death(self):
        persistent_flags = SpellAttributes.SPELL_ATTR_ALLOW_CAST_WHILE_DEAD | SpellAttributes.SPELL_ATTR_PASSIVE
        for aura in list(self.active_auras.values()):
            if aura.source_spell.spell_entry.Attributes & persistent_flags:
                continue
            self.remove_aura(aura)

    def remove_harmful_auras_by_caster(self, caster_guid):
        for aura in list(self.active_auras.values()):
            if aura.harmful and aura.caster.guid == caster_guid:
                self.remove_aura(aura)

    def remove_aura(self, aura, canceled=False):
        AuraEffectHandler.handle_aura_effect_change(aura, aura.target, remove=True)
        if not self.active_auras.pop(aura.index, None):
            return
        # Some area effect auras (paladin auras, tranq etc.) are tied to spell effects. Cancel cast on aura cancel, canceling the auras as well.
        self.unit_mgr.spell_manager.remove_cast(aura.source_spell, interrupted=canceled)

        # Some spells start cooldown on aura remove, handle that case here.
        if aura.source_spell.unlock_cooldown_on_trigger():
            self.unit_mgr.spell_manager.unlock_spell_cooldown(aura.spell_id)

        self.write_aura_to_unit(aura, clear=True)

    def remove_all_auras(self):
        for aura in list(self.active_auras.values()):
            self.remove_aura(aura)

    def cancel_auras_by_spell_id(self, spell_id):
        auras = self.get_auras_by_spell_id(spell_id)

        for aura in auras:
            self.remove_aura(aura, canceled=True)

    def build_update(self):
        [self.write_aura_to_unit(aura, send_duration=False) for aura in list(self.active_auras.values())]

    def handle_player_cancel_aura_request(self, spell_id):
        auras = self.get_auras_by_spell_id(spell_id)
        can_remove = True
        is_passive = True  # Player shouldn't be able to remove auras with only a passive part.
        for aura in auras:
            if not aura.passive:
                is_passive = False
            if aura.harmful or aura.source_spell.spell_entry.Attributes & SpellAttributes.SPELL_ATTR_CANT_CANCEL:
                can_remove = False  # Can't remove harmful auras.
                break

        if is_passive or not can_remove:
            return

        self.cancel_auras_by_spell_id(spell_id)

    def send_aura_duration(self, aura):
        if self.unit_mgr.get_type_id() != ObjectTypeIds.ID_PLAYER:
            return

        data = pack('<Bi', aura.index, int(aura.get_duration()))
        self.unit_mgr.enqueue_packet(PacketWriter.get_packet(OpCode.SMSG_UPDATE_AURA_DURATION, data))

    def write_aura_to_unit(self, aura, clear=False, is_refresh=False, send_duration=True):
        if aura.passive:
            return  # Passive auras are server-side only.

        if send_duration:
            self.send_aura_duration(aura)

        if is_refresh:
            # When refreshing auras, only a duration update is sent.
            return

        field_index = UnitFields.UNIT_FIELD_AURA + aura.index
        self.unit_mgr.set_uint32(field_index, aura.spell_id if not clear else 0)
        self._write_aura_flag_to_unit(aura, clear)

    def _write_aura_flag_to_unit(self, aura, clear=False):
        if not aura:
            return
        byte = (aura.index & 7) << 2  # magic value for AuraFlags.
        if not clear:
            self.current_flags |= 0x9 << byte  # OR to current flags - from other server's source.
        else:
            self.current_flags &= ~(0x9 << byte)

        field_index = UnitFields.UNIT_FIELD_AURAFLAGS + (aura.index >> 3)
        self.unit_mgr.set_uint32(field_index, self.current_flags)

    def get_next_aura_index(self, aura) -> int:
        if aura.passive:
            min_index = AuraSlots.AURA_SLOT_PASSIVE_AURA_START
            max_index = AuraSlots.AURA_SLOT_END
        elif aura.harmful:
            min_index = AuraSlots.AURA_SLOT_HARMFUL_AURA_START
            max_index = AuraSlots.AURA_SLOT_PASSIVE_AURA_START
        else:
            min_index = AuraSlots.AURA_SLOT_POSITIVE_AURA_START
            max_index = AuraSlots.AURA_SLOT_HARMFUL_AURA_START

        for i in range(min_index, max_index):
            if i not in self.active_auras:
                return i
        return min_index  # No aura slots free, return first possible. TODO Some kind of priority system?