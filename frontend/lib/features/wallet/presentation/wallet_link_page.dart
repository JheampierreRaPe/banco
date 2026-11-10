import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/app_text_field.dart';
import '../../../core/widgets/error_view.dart';
import '../../accounts/data/accounts_service.dart';
import '../../accounts/models/account.dart';
import '../data/wallet_service.dart';
import '../models/wallet.dart';

/// Fuente a vincular (selector del formulario).
enum WalletLinkKind { account, card }

/// Formulario de vinculacion de fuente de fondos (HU13 CA-01, E4-T04;
/// mockup pendiente `billetera-vinculacion.png`, G71; UI desde tokens
/// `docs/20`).
///
/// - **Cuenta del banco:** se elige de las cuentas del titular
///   (`GET /accounts`, decision 2 del brief); el cliente no teclea ni
///   valida numeros: envia el `account_id` y el backend valida.
/// - **Tarjeta:** captura el PAN crudo y lo envia (`card.pan`); el
///   backend lo tokeniza server-side y lo descarta. El PAN solo vive en
///   el controlador en memoria: se limpia al guardar con exito y al
///   salir (jamas en disco ni en logs; la UI solo muestra `last4`).
/// - `POST /wallet/links` crea el recurso -> la `Idempotency-Key` se
///   genera una vez por intento y el Reintentar reutiliza la misma
///   clave (sin duplicar el vinculo).
/// - Cliente delgado: muestra lo que responde el backend (vinculo
///   enmascarado `ACTIVE`); no calcula ni decide nada.
///
/// Estados: formulario, guardando, exito (vinculo enmascarado),
/// error accionable en espanol. Sin logica offline.
class WalletLinkPage extends StatefulWidget {
  const WalletLinkPage({super.key, this.service, this.accountsService});

  final WalletServiceBase? service;
  final AccountsServiceBase? accountsService;

  @override
  State<WalletLinkPage> createState() => _WalletLinkPageState();
}

class _WalletLinkPageState extends State<WalletLinkPage> {
  WalletLinkKind _kind = WalletLinkKind.account;
  final _panController = TextEditingController();

  Future<List<Account>>? _accountsFuture;
  String? _selectedAccountId;

  /// Clave del intento vigente: se genera una vez y se reutiliza en el
  /// Reintentar del mismo intento (CA: sin duplicar). "Vincular otra"
  /// inicia un intento nuevo con clave nueva.
  String _idempotencyKey = const Uuid().v4();

  bool _saving = false;
  String? _formError;
  String? _bannerCode;
  WalletLink? _saved;

  @override
  void initState() {
    super.initState();
    _accountsFuture = _loadAccounts();
  }

  @override
  void dispose() {
    // El PAN nunca persiste: se limpia al salir de la pantalla.
    _panController.clear();
    _panController.dispose();
    super.dispose();
  }

  Future<List<Account>> _loadAccounts() {
    final service = widget.accountsService;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de cuentas no configurado.',
        ),
      );
    }
    return service.getAccounts();
  }

  void _retryAccounts() {
    setState(() {
      _accountsFuture = _loadAccounts();
    });
  }

  String _digits(String value) => value.replaceAll(RegExp(r'\D'), '');

  bool _validateLocal() {
    if (_kind == WalletLinkKind.account) {
      if ((_selectedAccountId ?? '').isEmpty) {
        setState(() {
          _formError = 'Elige una de tus cuentas para vincularla.';
        });
        return false;
      }
      return true;
    }
    // Tarjeta: solo formato (longitud/digitos) en la UI; el backend
    // decide las reglas (Luhn, marca, duplicado).
    final digits = _digits(_panController.text);
    if (digits.length < 13 || digits.length > 19) {
      setState(() {
        _formError = 'Ingresa los dígitos de tu tarjeta (13 a 19).';
      });
      return false;
    }
    return true;
  }

  Future<void> _save() async {
    if (_saving) return;
    if (!_validateLocal()) return;
    final service = widget.service;
    if (service == null) {
      setState(() {
        _formError = 'Servicio de billetera no configurado.';
      });
      return;
    }
    setState(() {
      _saving = true;
      _formError = null;
      _bannerCode = null;
    });
    try {
      final WalletLink link;
      if (_kind == WalletLinkKind.account) {
        link = await service.linkAccount(
          AccountLinkInput(accountId: _selectedAccountId!),
          idempotencyKey: _idempotencyKey,
        );
      } else {
        // El PAN viaja solo en este request; se limpia en cuanto hay
        // respuesta (exito o error con Reintentar: el reintento reusa la
        // misma clave y el mismo cuerpo ya enviado al servidor).
        final pan = _digits(_panController.text);
        link = await service.linkCard(
          CardLinkInput(pan: pan),
          idempotencyKey: _idempotencyKey,
        );
        _panController.clear();
      }
      if (!mounted) return;
      setState(() {
        _saved = link;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        // DUPLICATE_LINK muestra banner dedicado con su codigo visible
        // (patron de `beneficiary_form_page`); el resto, mensaje bajo el
        // formulario. El Reintentar conserva `_idempotencyKey`.
        if (e.code == 'DUPLICATE_LINK') {
          _bannerCode = e.code;
          _formError = 'Esa fuente ya está vinculada a tu billetera.';
        } else {
          _formError = _friendlyLinkError(e.code, e.message);
        }
      });
    } finally {
      if (mounted) {
        setState(() {
          _saving = false;
        });
      }
    }
  }

  /// Vuelve a la billetera con resultado; sin pila (deep-link) navega a
  /// `/wallet` en vez de romper.
  void _closeWithResult(bool result) {
    if (context.canPop()) {
      context.pop(result);
    } else {
      context.go('/wallet');
    }
  }

  void _linkAnother() {
    // Intento nuevo: clave nueva + PAN limpio (nunca se reutiliza el
    // dato sensible entre intentos).
    _panController.clear();
    setState(() {
      _saved = null;
      _formError = null;
      _bannerCode = null;
      _selectedAccountId = null;
      _idempotencyKey = const Uuid().v4();
    });
  }

  @override
  Widget build(BuildContext context) {
    final saved = _saved;
    if (saved != null) {
      return _SuccessBody(
        link: saved,
        onViewWallet: () => _closeWithResult(true),
        onLinkAnother: _linkAnother,
      );
    }
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Vincular fuente')),
      body: Column(
        children: [
          Expanded(
            child: SingleChildScrollView(
              key: const Key('wallet-link-scroll'),
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Elige una cuenta del banco o una tarjeta. Solo se '
                    'muestra y guarda el dato enmascarado.',
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.secondaryText,
                    ),
                  ),
                  const SizedBox(height: 12),
                  _KindTabs(
                    kind: _kind,
                    onChanged: (kind) => setState(() {
                      _kind = kind;
                      _formError = null;
                      _bannerCode = null;
                    }),
                  ),
                  const SizedBox(height: 16),
                  if (_bannerCode != null) ...[
                    _DuplicateBanner(
                      code: _bannerCode!,
                      onViewWallet: () => _closeWithResult(true),
                      onDismiss: () =>
                          setState(() => _bannerCode = null),
                    ),
                    const SizedBox(height: 16),
                  ],
                  if (_kind == WalletLinkKind.account)
                    _AccountPicker(
                      future: _accountsFuture,
                      selectedId: _selectedAccountId,
                      onSelected: (id) => setState(() {
                        _selectedAccountId = id;
                        _formError = null;
                      }),
                      onRetry: _retryAccounts,
                    )
                  else
                    AppTextField(
                      key: const Key('wallet-link-pan'),
                      label: 'Número de tarjeta',
                      controller: _panController,
                      hintText: '•••• •••• •••• 1234',
                      keyboardType: TextInputType.number,
                      textInputAction: TextInputAction.done,
                      obscureText: true,
                      onChanged: (_) {
                        if (_formError != null) {
                          setState(() => _formError = null);
                        }
                      },
                    ),
                  if (_formError != null) ...[
                    const SizedBox(height: 12),
                    Container(
                      key: const Key('wallet-link-error'),
                      width: double.infinity,
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: AppColors.errorContainer,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Text(
                        _formError!,
                        style: AppTypography.labelMd.copyWith(
                          fontSize: 13,
                          color: AppColors.onErrorContainer,
                        ),
                      ),
                    ),
                  ],
                  const SizedBox(height: 16),
                  Container(
                    padding: const EdgeInsets.all(14),
                    decoration: BoxDecoration(
                      color: AppColors.surfaceContainerLow,
                      borderRadius: BorderRadius.circular(12),
                      border: const Border(
                        left: BorderSide(
                          color: AppColors.secondaryContainer,
                          width: 2,
                        ),
                      ),
                    ),
                    child: Text(
                      'El número completo nunca se guarda en este '
                      'dispositivo: viaja al servidor, que lo tokeniza y '
                      'devuelve solo los últimos 4 dígitos.',
                      style: AppTypography.bodyMd.copyWith(
                        fontSize: 12,
                        color: AppColors.onSurfaceVariant,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
              child: SizedBox(
                width: double.infinity,
                height: 52,
                child: FilledButton.icon(
                  key: const Key('wallet-link-save'),
                  onPressed: _saving ? null : _save,
                  icon: _saving
                      ? const SizedBox.square(
                          dimension: 20,
                          child: CircularProgressIndicator(strokeWidth: 2.5),
                        )
                      : const Icon(Icons.link, size: 20),
                  label: const Text(
                    'Vincular',
                    style: TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Selector cuenta/tarjeta (tabs del formulario).
class _KindTabs extends StatelessWidget {
  const _KindTabs({required this.kind, required this.onChanged});

  final WalletLinkKind kind;
  final ValueChanged<WalletLinkKind> onChanged;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(4),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerHigh,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          _KindTab(
            key: const Key('wallet-link-kind-account'),
            label: 'Cuenta del banco',
            selected: kind == WalletLinkKind.account,
            onTap: () => onChanged(WalletLinkKind.account),
          ),
          _KindTab(
            key: const Key('wallet-link-kind-card'),
            label: 'Tarjeta',
            selected: kind == WalletLinkKind.card,
            onTap: () => onChanged(WalletLinkKind.card),
          ),
        ],
      ),
    );
  }
}

class _KindTab extends StatelessWidget {
  const _KindTab({
    super.key,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: GestureDetector(
        onTap: onTap,
        child: Container(
          height: 36,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: selected
                ? AppColors.surfaceContainerLowest
                : Colors.transparent,
            borderRadius: BorderRadius.circular(8),
          ),
          child: Text(
            label,
            style: AppTypography.labelMd.copyWith(
              fontSize: 12,
              fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
              color: selected
                  ? AppColors.primary
                  : AppColors.onSurfaceVariant,
            ),
          ),
        ),
      ),
    );
  }
}

/// Selector de cuenta del titular (de `GET /accounts`; cliente delgado:
/// el servidor decide cuales existen y su estado).
class _AccountPicker extends StatelessWidget {
  const _AccountPicker({
    required this.future,
    required this.selectedId,
    required this.onSelected,
    required this.onRetry,
  });

  final Future<List<Account>>? future;
  final String? selectedId;
  final ValueChanged<String?> onSelected;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<Account>>(
      future: future,
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const Padding(
            padding: EdgeInsets.symmetric(vertical: 24),
            child: Center(child: CircularProgressIndicator()),
          );
        }
        if (snapshot.hasError) {
          final error = snapshot.error;
          return ErrorView(
            message: error is ApiException
                ? error.message
                : 'No pudimos cargar tus cuentas. Inténtalo de nuevo.',
            retryLabel: 'Reintentar',
            onRetry: onRetry,
          );
        }
        final accounts = snapshot.data ?? const <Account>[];
        if (accounts.isEmpty) {
          return Container(
            width: double.infinity,
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLow,
              borderRadius: BorderRadius.circular(12),
            ),
            child: const Text(
              'No tienes cuentas disponibles para vincular. '
              'Puedes vincular una tarjeta.',
            ),
          );
        }
        return DropdownButtonFormField<String>(
          key: const Key('wallet-link-account-field'),
          initialValue: selectedId,
          decoration: InputDecoration(
            labelText: 'Cuenta',
            filled: true,
            fillColor: AppColors.surfaceContainerLowest,
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(12),
            ),
          ),
          items: [
            for (final a in accounts)
              DropdownMenuItem(
                value: a.id,
                child: Text(
                  '${a.accountNumberMasked} · ${a.currency}',
                  overflow: TextOverflow.ellipsis,
                ),
              ),
          ],
          onChanged: onSelected,
        );
      },
    );
  }
}

/// Exito de la vinculacion: vinculo enmascarado `ACTIVE` tal cual del
/// backend (nunca el numero completo).
class _SuccessBody extends StatelessWidget {
  const _SuccessBody({
    required this.link,
    required this.onViewWallet,
    required this.onLinkAnother,
  });

  final WalletLink link;
  final VoidCallback onViewWallet;
  final VoidCallback onLinkAnother;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Vincular fuente')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            const SizedBox(height: 32),
            Container(
              key: const Key('wallet-link-success'),
              width: double.infinity,
              padding: const EdgeInsets.all(20),
              decoration: BoxDecoration(
                color: AppColors.surfaceContainerLowest,
                borderRadius: BorderRadius.circular(16),
              ),
              child: Column(
                children: [
                  const Icon(
                    Icons.check_circle_outline,
                    size: 48,
                    color: AppColors.primary,
                  ),
                  const SizedBox(height: 12),
                  const Text(
                    'Fuente vinculada',
                    style: TextStyle(
                      fontSize: 20,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(link.maskedLabel),
                  const SizedBox(height: 8),
                  const AppStatusChip.success(label: 'Activa'),
                ],
              ),
            ),
            const Spacer(),
            SizedBox(
              width: double.infinity,
              height: 52,
              child: FilledButton(
                key: const Key('wallet-link-view-wallet'),
                onPressed: onViewWallet,
                child: const Text('Ver billetera'),
              ),
            ),
            const SizedBox(height: 8),
            SizedBox(
              width: double.infinity,
              height: 52,
              child: OutlinedButton(
                key: const Key('wallet-link-another'),
                onPressed: onLinkAnother,
                child: const Text('Vincular otra'),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Banner de duplicado con codigo visible (patron de beneficiarios).
class _DuplicateBanner extends StatelessWidget {
  const _DuplicateBanner({
    required this.code,
    required this.onViewWallet,
    required this.onDismiss,
  });

  final String code;
  final VoidCallback onViewWallet;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context) {
    return Container(
      key: const Key('wallet-duplicate-banner'),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: AppColors.warningContainer,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  code,
                  style: const TextStyle(
                    fontSize: 10,
                    color: AppColors.onWarningContainer,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: FilledButton(
                  key: const Key('wallet-duplicate-view'),
                  style: FilledButton.styleFrom(
                    backgroundColor: AppColors.primaryContainer,
                  ),
                  onPressed: onViewWallet,
                  child: const Text(
                    'Ver billetera',
                    style: TextStyle(fontSize: 12),
                  ),
                ),
              ),
              TextButton(
                key: const Key('wallet-duplicate-dismiss'),
                onPressed: onDismiss,
                child: const Text(
                  'Ignorar',
                  style: TextStyle(
                    fontSize: 12,
                    color: AppColors.onWarningContainer,
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

/// Mensajes accionables por codigo de negocio (sin tocar `core`).
String _friendlyLinkError(String code, String fallback) {
  switch (code) {
    case 'VALIDATION_ERROR':
      return 'Revisa los datos ingresados e inténtalo de nuevo.';
    case 'NOT_FOUND':
      return 'No encontramos esa cuenta. Elige otra o verifica su estado.';
    case 'IDEMPOTENCY_CONFLICT':
      return 'Esta operación ya fue registrada con otros datos. '
          'Revisa tu billetera.';
    case 'NOT_AUTHORIZED':
      return 'No tienes permiso para esta operación.';
    case 'RATE_LIMITED':
      return 'Demasiados intentos. Espera un momento e inténtalo de nuevo.';
    default:
      return fallback;
  }
}
