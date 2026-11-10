// Validadores UI del PIN (F-T39): formato local, sin logica de negocio.
import 'package:banca_online/features/pin_setup/pin_setup_validators.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('pinCreateError', () {
    test('acepta un PIN de 6 digitos no trivial', () {
      expect(pinCreateError('482916'), isNull);
    });

    test('rechaza longitudes distintas de 6', () {
      expect(pinCreateError('1234'), pinLengthMessage);
      expect(pinCreateError('12345'), pinLengthMessage);
      expect(pinCreateError('1234567'), pinLengthMessage);
      expect(pinCreateError(''), pinLengthMessage);
    });

    test('rechaza no digitos', () {
      expect(pinCreateError('12a456'), pinDigitsMessage);
    });

    test('rechaza secuencias triviales', () {
      expect(pinCreateError('123456'), pinWeakMessage);
      expect(pinCreateError('654321'), pinWeakMessage);
      expect(pinCreateError('111111'), pinWeakMessage);
      expect(pinCreateError('000000'), pinWeakMessage);
    });
  });

  group('pinConfirmError', () {
    test('acepta la confirmacion identica', () {
      expect(pinConfirmError('482916', '482916'), isNull);
    });

    test('el desajuste bloquea', () {
      expect(pinConfirmError('482916', '482917'), pinMismatchMessage);
    });

    test('la confirmacion incompleta pide 6 digitos', () {
      expect(pinConfirmError('482916', '48291'), pinLengthMessage);
    });
  });

  group('sin PII en mensajes', () {
    test('ningun mensaje devuelve el PIN', () {
      const pin = '482916';
      for (final message in [
        pinLengthMessage,
        pinDigitsMessage,
        pinWeakMessage,
        pinMismatchMessage,
      ]) {
        expect(message, isNot(contains(pin)));
      }
      expect(pinCreateError('12'), isNot(contains('12')));
    });
  });
}
